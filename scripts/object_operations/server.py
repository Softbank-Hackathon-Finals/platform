"""Authenticated HTTP adapter over the same object-operation state and executor."""
import hashlib
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .worker import ACTIVE, API, Failure, encode
else:
    from worker import ACTIVE, API, Failure, encode

BASE = "/v1/object-exposure"
AUDIENCE = "https://kubernetes.default.svc.cluster.local"


class HTTPError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def receipt(value, run_id, owner):
    request = value.get("request", {})
    if request.get("run_id") == run_id and request.get("owner") == owner:
        return {"run_id": run_id, "owner": owner, "action": request["action"],
                "requested_at": request["requested_at"],
                "request_fingerprint": request.get("request_fingerprint"),
                "idempotency_key": request.get("idempotency_key"),
                **value.get("report", {}), "phase": value["phase"],
                "cancel_requested": bool(request.get("cancel"))}
    for item in reversed(value.get("history", [])):
        if item.get("run_id") == run_id and item.get("owner") == owner:
            return {**item, "phase": "FINISHED"}
    raise HTTPError(404, "run_not_found")


class Operations:
    def __init__(self, api):
        self.api = api
        self.lock = threading.Lock()

    def authenticate(self, token):
        if not token or len(token) > 16384:
            raise HTTPError(401, "personal_token_required")
        result = self.api.call("POST", "/apis/authentication.k8s.io/v1/tokenreviews", {
            "apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
            "spec": {"token": token, "audiences": [AUDIENCE]},
        }).get("status", {})
        if result.get("authenticated") is not True or AUDIENCE not in result.get("audiences", []):
            raise HTTPError(401, "invalid_or_expired_token")
        owner = result.get("user", {}).get("username", "")
        if not re.fullmatch(r"system:serviceaccount:bravo-team-access:member-0[1-5]", owner):
            raise HTTPError(403, "team_member_required")
        return owner

    def state(self):
        doc, value = self.api.state()
        observed = self.api.call("GET", "/api/v1/namespaces/kube-system")
        if observed["metadata"]["uid"] != value["config"]["cluster_uid"]:
            raise HTTPError(503, "cluster_uid_changed")
        return doc, value

    def submit(self, owner, key, body):
        match = re.fullmatch(r"([0-9]{10})-([a-f0-9]{32})", key or "")
        if match is None:
            raise HTTPError(400, "idempotency_key_must_be_epoch_seconds_and_uuid_hex")
        if not isinstance(body, dict) or set(body) - {"action", "hold_seconds", "acknowledge_anonymous_read"}:
            raise HTTPError(400, "unknown_request_field")
        action, hold, acknowledge = body.get("action"), body.get("hold_seconds", 15), body.get("acknowledge_anonymous_read", False)
        if action not in ("check", "cycle", "open", "close") or type(hold) is not int or not 0 <= hold <= 60 or type(acknowledge) is not bool:
            raise HTTPError(400, "invalid_operation")
        if action in ("cycle", "open") and not acknowledge:
            raise HTTPError(400, "anonymous_read_acknowledgement_required")
        fingerprint = hashlib.sha256(encode({"action": action, "hold_seconds": hold, "acknowledge_anonymous_read": acknowledge})).hexdigest()
        run_id = "objects-" + hashlib.sha256((owner + "/" + key).encode()).hexdigest()[:32]
        with self.lock:
            doc, value = self.state()
            try:
                existing = receipt(value, run_id, owner)
            except HTTPError as error:
                if error.status != 404:
                    raise
            else:
                if existing.get("request_fingerprint") != fingerprint:
                    raise HTTPError(409, "idempotency_key_reused_with_different_input")
                return 200, {**existing, "replayed": False, "existing_request": True}
            # The timestamp is part of the key. Even after receipt retention ends,
            # an old key cannot execute again by changing the request body/time.
            age = time.time() - int(match[1])
            if age < -60 or age > 300:
                raise HTTPError(410, "idempotency_window_expired")
            if value["phase"] in ACTIVE:
                raise HTTPError(409, "another_operation_active")
            if action == "cycle" and value.get("desired_state") == "open":
                raise HTTPError(409, "open_work_mode_requires_explicit_close")
            if action in ("open", "close"):
                value["desired_state"] = "open" if action == "open" else "closed"
            value.pop("recovery", None)
            value.update(phase="PENDING", report={}, request={
                "run_id": run_id, "owner": owner, "action": action, "hold_seconds": hold,
                "acknowledge_anonymous_read": acknowledge,
                "requested_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "idempotency_key": key, "request_fingerprint": fingerprint,
            })
            self.api.save(doc, value)
            return 202, {**receipt(value, run_id, owner), "replayed": False, "existing_request": False}

    def status(self):
        _, value = self.state()
        result = value.get("report", {})
        return {"desired_state": value.get("desired_state", "closed"),
                "last_observed_state": result.get("final_state"),
                "observed_at": result.get("finished_at"),
                "security_state": result.get("security_state"),
                "operation_in_progress": value["phase"] in ACTIVE,
                "fresh_check": "POST runs with action=check; this GET does not probe storage"}

    def get(self, owner, run_id):
        _, value = self.state()
        return receipt(value, run_id, owner)

    def list(self, owner):
        _, value = self.state()
        entries = {item["run_id"]: {**item, "phase": "FINISHED"} for item in value.get("history", []) if item.get("owner") == owner}
        if value.get("request", {}).get("owner") == owner:
            current = receipt(value, value["request"]["run_id"], owner)
            entries[current["run_id"]] = current
        return {"runs": list(reversed(list(entries.values()))), "retention": 64}

    def cancel(self, owner, run_id):
        with self.lock:
            doc, value = self.state()
            result = receipt(value, run_id, owner)
            if result["phase"] == "FINISHED":
                return result
            value["request"]["cancel"] = True
            if value["request"]["action"] == "open":
                value["desired_state"] = "closed"
            self.api.save(doc, value)
            return receipt(value, run_id, owner)


class Handler(BaseHTTPRequestHandler):
    operations = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # Never log a request header, token, raw body or provider response.

    def send(self, status, body):
        payload = encode(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def handle_request(self):
        self.connection.settimeout(15)
        path = urlsplit(self.path).path
        if self.command == "GET" and path == "/healthz":
            return self.send(200, {"service": "bravo-object-operations", "ready": True})
        authorization = self.headers.get("X-Bravo-Authorization", self.headers.get("Authorization", ""))
        if not authorization.startswith("Bearer "):
            raise HTTPError(401, "personal_token_required")
        owner = self.operations.authenticate(authorization[7:])
        if self.command == "GET" and path == "/openapi.json":
            return self.send(200, json.loads(Path(__file__).with_name("openapi.json").read_text()))
        if self.command == "GET" and path == BASE + "/status":
            return self.send(200, self.operations.status())
        body = None
        if self.command == "POST":
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise HTTPError(400, "json_content_length_required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 4096:
                raise HTTPError(413, "request_too_large")
            body = json.loads(self.rfile.read(length) or b"{}")
        if path == BASE + "/runs":
            if self.command == "GET":
                return self.send(200, self.operations.list(owner))
            if self.command == "POST":
                status, value = self.operations.submit(owner, self.headers.get("Idempotency-Key"), body)
                return self.send(status, value)
        match = re.fullmatch(re.escape(BASE) + r"/runs/(objects-[a-f0-9]{32})(/cancel)?", path)
        if match:
            if self.command == "GET" and not match[2]:
                return self.send(200, self.operations.get(owner, match[1]))
            if self.command == "POST" and match[2] and body == {}:
                return self.send(202, self.operations.cancel(owner, match[1]))
        raise HTTPError(404, "route_not_found")

    def dispatch(self):
        try:
            self.handle_request()
        except HTTPError as error:
            self.send(error.status, {"error": error.code})
        except (ValueError, TypeError):
            self.send(400, {"error": "invalid_json_request"})
        except Failure as error:
            self.send(409 if str(error) in ("kubernetes_409", "kubernetes_422") else 503,
                      {"error": "state_changed_retry_same_idempotency_key" if str(error) in ("kubernetes_409", "kubernetes_422") else "backend_unavailable"})
        except (OSError, KeyError):
            self.send(503, {"error": "backend_unavailable"})

    do_GET = dispatch
    do_POST = dispatch


if __name__ == "__main__":
    Handler.operations = Operations(API())
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
