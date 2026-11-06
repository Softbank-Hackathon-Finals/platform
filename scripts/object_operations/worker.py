"""In-cluster executor. No credentials, object bodies or provider errors leave this process."""

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import object_exposure

NAMESPACE = "target-dev"
STATE = "bravo-object-operations"
SECRET = "target-seaweedfs-auth"
DEPLOYMENT = "target-seaweedfs"
BUCKET = "demo-chat-attachments"
OTHER_BUCKET = "rehearsal-attachments"
ENDPOINT = "http://seaweedfs.target-dev.svc.cluster.local:8333"
BODY = b"Bravo object operations disposable probe\n"
CORE = f"/api/v1/namespaces/{NAMESPACE}"
APPS = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}"
ACTIVE = {"PENDING", "RUNNING", "CLOSING", "RECOVERY_REQUIRED"}
RECOVERY_HISTORY_LIMIT = 8


class Failure(Exception):
    pass


def now():
    return datetime.now(UTC).isoformat()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class API:
    def __init__(self):
        root = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.token_file = root / "token"
        context = ssl.create_default_context(cafile=str(root / "ca.crt"))
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
            object_exposure._NoRedirect(),
        )

    def call(self, method, path, body=None, patch=False):
        request = urllib.request.Request(
            "https://kubernetes.default.svc" + path, method=method,
            data=None if body is None else encode(body),
            headers={"Authorization": "Bearer " + self.token_file.read_text().strip(),
                     "Content-Type": "application/json-patch+json" if patch else "application/json"},
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise Failure(f"kubernetes_{error.code}") from None
        except (OSError, http.client.HTTPException):
            raise Failure("kubernetes_unavailable") from None

    def state(self):
        doc = self.call("GET", f"{CORE}/configmaps/{STATE}")
        return doc, json.loads(doc["data"]["state.json"])

    def save(self, doc, value):
        return self.call("PATCH", f"{CORE}/configmaps/{STATE}", [
            {"op": "test", "path": "/metadata/uid", "value": doc["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": doc["metadata"]["resourceVersion"]},
            {"op": "replace", "path": "/data/state.json", "value": encode(value).decode()},
        ], patch=True)


class S3:
    def __init__(self, endpoint=ENDPOINT):
        self.endpoint = endpoint
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), object_exposure._NoRedirect())

    def call(self, method, bucket, key="", *, query=(), body=b"", credentials=None):
        path = "/" + bucket + ("/" + key if key else "")
        path = urllib.parse.quote(path, safe="/-_.~")
        query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(query))
        headers = {}
        if credentials:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            payload = hashlib.sha256(body).hexdigest()
            headers = {"host": urllib.parse.urlsplit(self.endpoint).netloc,
                       "x-amz-content-sha256": payload, "x-amz-date": stamp}
            names = ";".join(sorted(headers))
            canonical = "\n".join([method, path, query, *(f"{k}:{headers[k]}" for k in sorted(headers)), "", names, payload])
            scope = f"{stamp[:8]}/us-east-1/s3/aws4_request"
            to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
            signing = ("AWS4" + credentials["secretKey"]).encode()
            for part in (stamp[:8], "us-east-1", "s3", "aws4_request"):
                signing = hmac.new(signing, part.encode(), hashlib.sha256).digest()
            signature = hmac.new(signing, to_sign.encode(), hashlib.sha256).hexdigest()
            headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={credentials['accessKey']}/{scope}, SignedHeaders={names}, Signature={signature}"
        request = urllib.request.Request(self.endpoint + path + ("?" + query if query else ""), method=method,
                                         data=body if method in ("PUT", "POST") else None, headers=headers)
        try:
            with self.opener.open(request, timeout=8) as response:
                return response.status, None, response.read(1 << 20)
        except urllib.error.HTTPError as error:
            raw = error.read(65536)
            code = raw.split(b"<Code>", 1)[-1].split(b"</Code>", 1)[0] if b"<Code>" in raw else b""
            return error.code, "NoSuchBucketPolicy" if code == b"NoSuchBucketPolicy" else None, b""
        except (OSError, http.client.HTTPException):
            return "UNAVAILABLE", None, b""


def render_existing(document, state, run_id):
    """Accept only the existing T9 contract; refuse to discard unknown identities or rotations."""
    aliases = {"target-object-bootstrap": "bootstrap", "target-object-app": "client"}
    original_names = {aliases.get(item.get("name"), item.get("name")): item.get("name") for item in document.get("identities", [])}
    identities = [{**item, "name": aliases.get(item.get("name"), item.get("name"))} for item in document.get("identities", [])]
    document = {**document, "identities": identities}
    names = [identity.get("name") for identity in identities]
    expected = {"bootstrap", "client"}
    if "rehearsal-object-admin" in names:
        expected.add("rehearsal-object-admin")
    if len(names) != len(set(names)) or set(names) - {"anonymous"} != expected:
        raise Failure("unsupported_identity_layout")
    env = {}
    credentials = {}
    for identity in identities:
        name = identity["name"]
        if name == "anonymous":
            continue
        values = identity.get("credentials", [])
        if len(values) != 1 or set(values[0]) != {"accessKey", "secretKey"}:
            raise Failure("unsupported_credential_layout")
        pair = object_exposure.REHEARSAL_ENV if name == "rehearsal-object-admin" else object_exposure.ENV[name]
        env[pair[0]], env[pair[1]] = values[0]["accessKey"], values[0]["secretKey"]
        credentials[name] = values[0]
    options = {"rehearsal_bucket": OTHER_BUCKET if "rehearsal-object-admin" in names else None}
    current_state = "open-demo" if "anonymous" in names else "closed"
    reference = object_exposure.render(current_state, BUCKET, env, acknowledge_open=True, demo_run_id=run_id, **options)
    # Identity order is not semantic; every other field must match the owning renderer.
    normalize = lambda doc: {**doc, "identities": sorted(doc["identities"], key=lambda item: item["name"])}
    if normalize(document) != normalize(reference):
        raise Failure("existing_policy_differs_from_t9_contract")
    result = object_exposure.render(state, BUCKET, env, acknowledge_open=True, demo_run_id=run_id, **options)
    result["identities"] = [{**item, "name": original_names.get(item["name"], item["name"])} for item in result["identities"]]
    return result, credentials, current_state


def validate_request(request):
    if not re.fullmatch(r"objects-[a-f0-9]{32}", request.get("run_id", "")):
        raise Failure("invalid_run_id")
    if request.get("action") not in ("cycle", "open", "close", "check"):
        raise Failure("invalid_action")
    if not re.fullmatch(r"system:serviceaccount:bravo-team-access:member-0[1-5]", request.get("owner", "")):
        raise Failure("personal_member_required")
    if type(request.get("hold_seconds")) is not int or not 0 <= request["hold_seconds"] <= 60:
        raise Failure("invalid_hold")
    if request["action"] in ("cycle", "open") and request.get("acknowledge_anonymous_read") is not True:
        raise Failure("anonymous_read_acknowledgement_required")
    expiry = datetime.fromisoformat(request["expires_at"]).timestamp()
    if not time.time() < expiry <= time.time() + 600:
        raise Failure("execution_window_expired")


class Worker:
    def __init__(self, api, pod_uid):
        self.api, self.pod_uid = api, pod_uid
        self.s3 = S3()
        self.report = {}

    def own(self):
        doc, value = self.api.state()
        if value.get("executor") != self.pod_uid or value.get("request", {}).get("run_id") != self.request["run_id"]:
            raise Failure("execution_fenced")
        return doc, value

    def persist(self, phase):
        for name in ("transitions", "previous_closure_checks", "recovery_executor_pod_uids", "previous_cleanup_errors"):
            if name in self.report:
                self.report[name] = self.report[name][-RECOVERY_HISTORY_LIMIT:]
        doc, value = self.own()
        value.update(phase=phase, report=self.report, updated_at=now())
        if getattr(self, "canary_key", None):
            value["recovery"] = {"canary_key": self.canary_key}
        self.api.save(doc, value)

    def guard(self):
        cluster = self.api.call("GET", "/api/v1/namespaces/kube-system")
        if cluster["metadata"]["uid"] != self.config["cluster_uid"]:
            raise Failure("cluster_uid_changed")
        secret = self.api.call("GET", f"{CORE}/secrets/{SECRET}")
        deployment = self.api.call("GET", APPS)
        if secret["metadata"]["uid"] != self.config["secret_uid"] or deployment["metadata"]["uid"] != self.config["deployment_uid"]:
            raise Failure("target_uid_changed")
        if deployment["spec"].get("replicas", 1) != 1:
            raise Failure("single_replica_required")
        self.report["target_image"] = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        auth = json.loads(base64.b64decode(secret["data"]["s3.json"]))
        self.report["identity_contract"] = [{"name": item.get("name"), "actions": item.get("actions"), "credential_count": len(item.get("credentials", []))} for item in auth.get("identities", [])]
        return secret, deployment, auth

    def case(self, name, actual, expected):
        self.report.setdefault("checks", []).append({"name": name, "actual": actual, "expected": expected, "pass": actual == expected})
        return actual == expected

    def transition(self, state):
        start = time.monotonic()
        secret, deployment, auth = self.guard()
        rendered, self.credentials, _ = render_existing(auth, state, self.request["run_id"])
        # Record possible effects before the first write, including response loss.
        self.report["effects_started"] = True
        self.persist("CLOSING" if state == "closed" else "RUNNING")
        self.own()  # Read target RV before fencing; recovery invalidates stale target writes.
        secret_annotations = secret["metadata"].get("annotations", {})
        secret_annotations["bravo.io/object-operation-executor"] = self.pod_uid
        self.api.call("PATCH", f"{CORE}/secrets/{SECRET}", [
            {"op": "test", "path": "/metadata/uid", "value": secret["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": secret["metadata"]["resourceVersion"]},
            {"op": "replace", "path": "/data/s3.json", "value": base64.b64encode(encode(rendered)).decode()},
            {"op": "add", "path": "/metadata/annotations", "value": secret_annotations},
        ], patch=True)
        self.own()
        deployment = self.api.call("GET", APPS)
        annotations = deployment["spec"]["template"]["metadata"].get("annotations", {})
        annotations["bravo.io/object-operation"] = self.request["run_id"] + "/" + state
        changed = self.api.call("PATCH", APPS, [
            {"op": "test", "path": "/metadata/uid", "value": self.config["deployment_uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
            {"op": "add", "path": "/spec/template/metadata/annotations", "value": annotations},
        ], patch=True)
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            self.own()
            live = self.api.call("GET", APPS)
            status = live.get("status", {})
            if live["metadata"]["uid"] != self.config["deployment_uid"]:
                raise Failure("target_uid_changed")
            if live["metadata"]["generation"] != changed["metadata"]["generation"]:
                raise Failure("deployment_changed_during_operation")
            if (status.get("observedGeneration", 0) >= changed["metadata"]["generation"]
                    and status.get("updatedReplicas") == 1 and status.get("readyReplicas") == 1
                    and status.get("replicas") == 1):
                self.report.setdefault("transitions", []).append({"state": state, "ready_seconds": round(time.monotonic() - start, 3)})
                return
            time.sleep(2)
        raise Failure("rollout_timeout")

    def probe(self, state):
        result = object_exposure.probe(ENDPOINT, BUCKET, self.key, opener=self.s3.opener.open, timeout=8)
        return self.case(f"{state}_t9_probe", result["state_observed"], state)

    def policy(self, remove=False):
        code, error, _ = self.s3.call("GET", BUCKET, query=[("policy", "")], credentials=self.credentials["bootstrap"])
        if code == 200 and remove:
            self.own()
            removed, _, _ = self.s3.call("DELETE", BUCKET, query=[("policy", "")], credentials=self.credentials["bootstrap"])
            if removed not in (200, 204):
                raise Failure("policy_delete_unconfirmed")
            self.report["removed_existing_bucket_policy"] = True
            code, error, _ = self.s3.call("GET", BUCKET, query=[("policy", "")], credentials=self.credentials["bootstrap"])
        if (code, error) != (404, "NoSuchBucketPolicy"):
            raise Failure("bucket_policy_not_absent")
        return True

    def app_client(self):
        app = self.api.call("GET", f"{CORE}/secrets/target-app-objects")
        credentials = {"accessKey": base64.b64decode(app["data"]["access_key"]).decode(),
                       "secretKey": base64.b64decode(app["data"]["secret_key"]).decode()}
        if credentials != self.credentials["client"]:
            raise Failure("application_secret_does_not_match_client_identity")
        self.report["application_secret_matches_store_client"] = True

    def existing_canary(self):
        status, _, body = self.s3.call("GET", BUCKET, query=[("list-type", "2"), ("max-keys", "100")], credentials=self.credentials["client"])
        if status != 200:
            raise Failure("existing_app_object_listing_unavailable")
        root = ET.fromstring(body)
        for item in root.findall("{*}Contents"):
            key = item.findtext("{*}Key")
            size = int(item.findtext("{*}Size", "0"))
            if key and not key.startswith("operations/object-exposure/") and 0 < size < (1 << 20):
                return key
        raise Failure("existing_app_canary_unavailable")

    def baseline(self):
        _, _, auth = self.guard()
        _, self.credentials, state = render_existing(auth, "closed", self.request["run_id"])
        self.report["initial_state"] = state
        if state != "closed" and not (self.request["action"] == "open" and state == "open-demo"):
            raise Failure("initial_state_not_closed")
        self.policy()
        self.app_client()
        self.canary_key = getattr(self, "canary_key", None) or self.existing_canary()
        status, _, body = self.s3.call("GET", BUCKET, self.canary_key, credentials=self.credentials["client"])
        if status != 200:
            raise Failure("existing_app_canary_unavailable")
        self.report["app_canary_sha256_before"] = hashlib.sha256(body).hexdigest()
        for key in self.keys:
            if self.s3.call("HEAD", BUCKET, key, credentials=self.credentials["client"])[0] != 404:
                raise Failure("disposable_key_already_present_or_unavailable")
        self.report["objects_owned"] = True
        self.report["effects_started"] = True
        self.persist("RUNNING")  # Persist intent before PUT, including response-loss recovery.
        if self.s3.call("PUT", BUCKET, self.key, body=BODY, credentials=self.credentials["client"])[0] != 200:
            raise Failure("probe_object_put_unconfirmed")
        if not self.case("initial_anonymous_get", self.s3.call("GET", BUCKET, self.key)[0], 200 if state == "open-demo" else 403):
            raise Failure("initial_runtime_not_closed")

    def opened(self):
        _, current = self.own()
        if current["request"].get("cancel"):
            raise Failure("cancelled_before_open")
        if self.report.get("initial_state") != "open-demo":
            self.transition("open-demo")
        self.report["opened_at"] = now()
        self.probe("open")
        for label, method, bucket, key, query, body, expected in (
            ("get", "GET", BUCKET, self.key, (), b"", 200),
            ("head", "HEAD", BUCKET, self.key, (), b"", 200),
            ("put", "PUT", BUCKET, self.keys[1], (), BODY, 403),
            ("list", "GET", BUCKET, "", (("list-type", "2"), ("prefix", self.prefix)), b"", 403),
            ("delete", "DELETE", BUCKET, self.key, (), b"", 403),
            ("other_bucket", "GET", OTHER_BUCKET, self.key, (), b"", 403),
        ):
            self.own()
            status = self.s3.call(method, bucket, key, query=query, body=body)[0]
            if not self.case("open_anonymous_" + label, status, expected):
                raise Failure("anonymous_boundary_mismatch")
        self.policy()
        self.persist("RUNNING")
        end = time.monotonic() + (self.request["hold_seconds"] if self.request["action"] == "cycle" else 0)
        while time.monotonic() < end:
            _, value = self.own()
            if value["request"].get("cancel"):
                self.report["cancel_requested"] = True
                break
            time.sleep(1)

    def cleanup_objects(self):
        if self.report.get("objects_owned"):
            for key in self.keys:
                self.own()
                status = self.s3.call("DELETE", BUCKET, key, credentials=self.credentials["client"])[0]
                if status not in (200, 204) or self.s3.call("HEAD", BUCKET, key, credentials=self.credentials["client"])[0] != 404:
                    raise Failure("owned_object_cleanup_unconfirmed")

    def keep_open(self):
        _, current = self.own()
        if current["request"].get("cancel"):
            raise Failure("cancelled_before_open_completion")
        _, _, auth = self.guard()
        _, self.credentials, state = render_existing(auth, "open-demo", self.request["run_id"])
        if state != "open-demo":
            raise Failure("configured_state_changed_after_open")
        self.app_client()
        self.policy()
        self.case("open_existing_attachment_anonymous_get", self.s3.call("GET", BUCKET, self.canary_key)[0], 200)
        status, _, body = self.s3.call("GET", BUCKET, self.canary_key, credentials=self.credentials["client"])
        self.case("open_existing_app_client_get", status, 200)
        self.case("open_existing_app_bytes_unchanged", hashlib.sha256(body).hexdigest(), self.report["app_canary_sha256_before"])
        if not all(case["pass"] for case in self.report["checks"]):
            raise Failure("open_readback_failed")
        self.cleanup_objects()
        self.report.update(cleanup="CLEAN", final_state="open", security_state="PUBLIC_READ", automatically_closes=False)

    def close(self):
        previous = [case for case in self.report["checks"] if case["name"].startswith("closed_")]
        if previous:
            self.report.setdefault("previous_closure_checks", []).append(previous)
            self.report["checks"] = [case for case in self.report["checks"] if not case["name"].startswith("closed_")]
        self.persist("CLOSING")
        self.transition("closed")
        self.policy(remove=True)
        self.case("closed_bucket_policy_absent", True, True)
        self.case("closed_anonymous_get", self.s3.call("GET", BUCKET, self.key)[0], 403)
        self.probe("closed")
        closed = [case for case in self.report["checks"] if case["name"].startswith("closed_")]
        if not all(case["pass"] for case in closed):
            raise Failure("closed_readback_failed")
        _, _, auth = self.guard()
        _, _, state = render_existing(auth, "closed", self.request["run_id"])
        if state != "closed":
            raise Failure("configured_state_changed_after_close")
        # App drift or a missing attachment cannot undo a verified closure. Preserve
        # the app failure, clean our own objects, and release the operation slot.
        self.report.update(closed_at=now(), final_state="closed", security_state="PRIVATE")
        try:
            self.app_client()
            self.canary_key = getattr(self, "canary_key", None) or self.existing_canary()
            status, _, body = self.s3.call("GET", BUCKET, self.canary_key, credentials=self.credentials["client"])
            self.case("closed_existing_app_client_get", status, 200)
            if "app_canary_sha256_before" in self.report:
                self.case("closed_existing_app_bytes_unchanged", hashlib.sha256(body).hexdigest(), self.report["app_canary_sha256_before"])
            if not all(case["pass"] for case in self.report["checks"] if case["name"].startswith("closed_existing_app_")):
                raise Failure("application_readback_failed")
            self.report["application_readback"] = "VERIFIED"
        except Exception as error:  # noqa: BLE001 — provider responses are never persisted.
            self.report["application_readback"] = "UNCONFIRMED"
            self.report["application_error"] = str(error) if isinstance(error, Failure) else type(error).__name__
            self.report.setdefault("error", self.report["application_error"])
        self.cleanup_objects()
        self.report["cleanup"] = "CLEAN"
        if "cleanup_error" in self.report:
            self.report.setdefault("previous_cleanup_errors", []).append(self.report.pop("cleanup_error"))

    def run(self):
        doc, value = self.api.state()
        phase = value.get("phase", "IDLE")
        if phase not in ACTIVE:
            return
        self.request, self.config = value["request"], value["config"]
        self.canary_key = value.get("recovery", {}).get("canary_key")
        self.key = f"operations/object-exposure/{self.request['run_id']}/probe.txt"
        self.prefix = self.key.rsplit("/", 1)[0] + "/"
        self.keys = [self.key, self.prefix + "anonymous-put.txt"]
        recovery = phase != "PENDING"
        self.report = value.get("report", {}) if recovery else {
            "schema": "bravo.object-operation/v1", "run_id": self.request["run_id"],
            "owner": self.request["owner"], "action": self.request["action"], "started_at": now(),
            "verdict": "UNCONFIRMED", "cleanup": "UNCONFIRMED", "checks": [],
            "effects_started": False,
            "executor_source_sha256": self.config.get("source_sha256"),
            "executor_pod_uid": self.pod_uid,
            "requested_at": self.request.get("requested_at"),
            "idempotency_key": self.request.get("idempotency_key"),
            "request_fingerprint": self.request.get("request_fingerprint"),
        }
        if recovery:
            self.report.setdefault("recovery_executor_pod_uids", []).append(self.pod_uid)
        value.update(executor=self.pod_uid, phase="RUNNING", report=self.report)
        self.api.save(doc, value)  # A failed CAS cannot start another operation.
        # Older in-flight receipts have no marker and require conservative closure.
        close_required = recovery and self.request["action"] != "check" and self.report.get("effects_started", True)
        try:
            if recovery:
                self.report["interrupted_operation_not_replayed"] = True
                self.report.setdefault("error", "interrupted")
            else:
                validate_request(self.request)
                if self.request.get("cancel"):
                    raise Failure("cancelled_before_start")
                if self.request["action"] == "cycle" and value.get("desired_state") == "open":
                    raise Failure("open_work_mode_requires_explicit_close")
                if self.request["action"] == "close":
                    # Reject unknown layouts/UID changes before any mutation. A
                    # terminal refusal must not trap the whole service in recovery.
                    _, _, auth = self.guard()
                    render_existing(auth, "closed", self.request["run_id"])
                    close_required = True
                if self.request["action"] == "check":
                    _, _, auth = self.guard()
                    _, self.credentials, state = render_existing(auth, "closed", self.request["run_id"])
                    self.app_client()
                    self.case("configured_state", state, "open-demo" if value.get("desired_state") == "open" else "closed")
                    self.case("bucket_policy_absent", self.policy(), True)
                    self.canary_key = self.existing_canary()
                    status = self.s3.call("GET", BUCKET, self.canary_key, credentials=self.credentials["client"])[0]
                    self.case("existing_app_client_get", status, 200)
                    self.case("anonymous_get", self.s3.call("GET", BUCKET, self.canary_key)[0], 200 if state == "open-demo" else 403)
                    self.report.update(final_state="open" if state == "open-demo" else "closed", security_state="PUBLIC_READ" if state == "open-demo" else "PRIVATE")
                    self.report["cleanup"] = "NOT_APPLICABLE"
                elif self.request["action"] in ("cycle", "open"):
                    self.baseline()
                    close_required = True
                    # Never open after the requested window expired during preparation.
                    validate_request(self.request)
                    self.opened()
                    if self.request["action"] == "open":
                        self.keep_open()
                        close_required = False
        except Exception as error:  # noqa: BLE001 — never persist provider exception bodies.
            self.report["error"] = str(error) if isinstance(error, Failure) else type(error).__name__
        finally:
            if self.report.get("effects_started") and self.report.get("cleanup") != "CLEAN":
                close_required = True
            if close_required:
                try:
                    self.close()
                except Exception as error:  # noqa: BLE001 — the next scheduled worker retries only closure.
                    self.report["cleanup_error"] = str(error) if isinstance(error, Failure) else type(error).__name__
                    if self.report.get("effects_started", True):
                        if self.report.get("error") or any(not c["pass"] for c in self.report["checks"]):
                            self.report["verdict"] = "FAIL"
                        self.persist("RECOVERY_REQUIRED")
                        return
                    self.report.setdefault("error", self.report["cleanup_error"])
                    self.report["cleanup"] = "NOT_APPLICABLE"
            if not close_required and self.report["cleanup"] == "UNCONFIRMED":
                self.report["cleanup"] = "NOT_APPLICABLE"
            self.report["finished_at"] = now()
            failed = any(not c["pass"] for c in self.report["checks"]) or self.report.get("error") not in (None, "interrupted")
            self.report["verdict"] = "FAIL" if failed else "UNCONFIRMED" if recovery else "PASS"
            doc, value = self.own()
            if not self.report.get("effects_started") and self.report.get("error"):
                value["desired_state"] = self.request.get("previous_desired_state", value.get("desired_state", "closed"))
            elif self.report.get("final_state") in ("open", "closed"):
                value["desired_state"] = self.report["final_state"]
            value.update(phase="FINISHED", report=self.report, updated_at=now())
            value["history"] = (value.get("history", []) + [self.report])[-64:]
            self.api.save(doc, value)
            print(encode(self.report).decode(), flush=True)


if __name__ == "__main__":
    try:
        Worker(API(), os.environ["POD_UID"]).run()
    except Exception as error:  # noqa: BLE001 — no raw API/Secret material in logs.
        print(json.dumps({"error": str(error) if isinstance(error, Failure) else type(error).__name__, "cleanup": "UNCONFIRMED"}), flush=True)
        raise SystemExit(1) from None
