"""Run inside the new dev app pod; exercise HTTP/DB/S3 and finish write-blocked.

Only synthetic canary records are added. No credential or response body is
printed. ADMIN_TOKEN stays in the process environment and memory.
"""

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(method, path, body=None, admin=False, headers=None):
    headers = dict(headers or {})
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if admin:
        headers["Authorization"] = "Bearer " + os.environ["ADMIN_TOKEN"]
    req = Request("http://127.0.0.1:8080" + path, data=body, headers=headers, method=method)
    try:
        response = urlopen(req, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, {key.lower(): value for key, value in response.headers.items()}, response.read(1024 * 1024)


def run():
    checks = {}

    def check(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise RuntimeError("probe check failed")

    def status(name, method, path, expected, **kwargs):
        result = request(method, path, **kwargs)
        check(name, result[0] == expected)
        return result

    result = {"schema_version": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "actual source app HTTP, Secrets Manager startup, PgBouncer DB and S3 canaries",
              "checks": checks, "canary_records_retained": True}
    opened = False
    try:
        _, _, raw = status("info_available", "GET", "/api/info", 200)
        info = json.loads(raw)
        check("aws_mode_initially_write_blocked", info["env"] == "aws" and info["write_blocked"])
        for path in ("/healthz", "/readyz", "/api/health/db"):
            status("initial" + path, "GET", path, 200)
        status("missing_admin_denied", "GET", "/admin/write-block", 401)
        status("wrong_admin_denied", "PUT", "/admin/write-block", 401,
               body={"blocked": True}, headers={"Authorization": "Bearer invalid-canary"})
        message = {"idempotency_key": str(uuid.uuid4()), "author": "T6 runtime canary",
                   "body": "Synthetic source readiness verification; no customer data."}
        _, headers, _ = status("initial_write_denied", "POST", "/api/messages", 503, body=message)
        check("initial_retry_after", bool(headers.get("retry-after")))
        # Set the restoration flag before the request: a timeout can occur after a successful change.
        opened = True
        status("enable_canary_writes", "PUT", "/admin/write-block", 200, body={"blocked": False}, admin=True)
        _, _, first = status("message_created", "POST", "/api/messages", 201, body=message)
        _, _, repeat = status("idempotent_retry", "POST", "/api/messages", 200, body=message)
        first, repeat = json.loads(first), json.loads(repeat)
        check("same_message_id", first["id"] == repeat["id"])
        status("changed_idempotency_payload_denied", "POST", "/api/messages", 409,
               body={**message, "body": "Different synthetic payload"})
        attachment_id = str(uuid.uuid4())
        data = b"T6 source attachment canary: synthetic bytes only.\n"
        boundary = "t6-canary-" + uuid.uuid4().hex
        multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t6-canary.txt\"\r\n"
                     "Content-Type: text/plain\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        status("attachment_uploaded", "PUT", "/api/attachments/" + attachment_id, 200, body=multipart,
               headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        _, headers, downloaded = status("attachment_downloaded", "GET", "/api/attachments/" + attachment_id, 200)
        check("attachment_bytes_match", hashlib.sha256(data).digest() == hashlib.sha256(downloaded).digest())
        check("attachment_nosniff", headers.get("x-content-type-options") == "nosniff")
        linked = {**message, "idempotency_key": str(uuid.uuid4()), "attachment_id": attachment_id}
        _, _, saved = status("message_attachment_linked", "POST", "/api/messages", 201, body=linked)
        saved = json.loads(saved)
        _, _, raw = status("business_read", "GET", "/api/messages?after=" + str(first["id"] - 1), 200)
        rows = json.loads(raw)
        check("written_rows_read_back", {first["id"], saved["id"]}.issubset({row["id"] for row in rows}))
        check("attachment_reference_read_back", any(row["id"] == saved["id"] and row["attachment_id"] == attachment_id for row in rows))
    except (OSError, HTTPException, ValueError, KeyError, TypeError, RuntimeError) as error:
        result["error_type"] = type(error).__name__
    finally:
        if opened:
            restored = False
            restore_errors = []
            for attempt in range(1, 4):
                # A timed-out PUT may already have applied. Always read back, and
                # retry only the idempotent block operation, never an unblock.
                try:
                    code, _, _ = request("PUT", "/admin/write-block", body={"blocked": True}, admin=True)
                    if code != 200:
                        restore_errors.append("PutStatus" + str(code))
                except (OSError, HTTPException, ValueError, KeyError, TypeError, RuntimeError) as error:
                    restore_errors.append(type(error).__name__)
                try:
                    code, _, raw = request("GET", "/admin/write-block", admin=True)
                    payload = json.loads(raw)
                    restored = code == 200 and isinstance(payload, dict) and payload.get("write_blocked") is True
                except (OSError, HTTPException, ValueError, KeyError, TypeError, RuntimeError) as error:
                    restore_errors.append(type(error).__name__)
                if restored:
                    break
            result["restore_attempts"] = attempt
            result["restore_transient_errors"] = restore_errors
            result["manual_write_block_required"] = not restored
            checks["restore_write_block"] = restored
            checks["final_admin_read"] = restored
            checks["final_write_blocked"] = restored
            try:
                if not restored:
                    raise RuntimeError("manual write block required")
                _, headers, _ = status("final_write_denied", "POST", "/api/messages", 503,
                                        body={"idempotency_key": str(uuid.uuid4()), "author": "T6 canary", "body": "Must be denied"})
                check("final_retry_after", bool(headers.get("retry-after")))
                for path in ("/healthz", "/readyz", "/api/health/db", "/api/messages"):
                    status("blocked" + path, "GET", path, 200)
            except (OSError, HTTPException, ValueError, KeyError, TypeError, RuntimeError) as error:
                result["restore_error_type"] = type(error).__name__
    result["passed"] = bool(checks) and all(checks.values()) and not any(key.endswith("error_type") for key in result)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(run())
