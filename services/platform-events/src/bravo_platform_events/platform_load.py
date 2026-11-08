"""Platform load for SHIFT and WATCH (T49 demo): T36 judges only windows a load produces, so the platform makes it.

Before this, `s3-load` was a person running `bravo-loadgen` on the loadgen host. Here the workflow host keeps one
low-rate Kubernetes Job per plan while it is in SHIFT, CUTOVER or WATCH and the approver delegated SHIFT. The Job
runs the same T18 code (`bravo-loadgen run --generator platform-job --window-stdout`) through the Edge; its windows
come back through the Job log and enter T10 by the same `WindowIntake.receive` (T36 validation, plan lock) under a
platform producer bound to the plan's current state. No token, tailnet producer or loadgen host is involved.

ponytail: windows are read from the log each sweep (tail); a window the T36 store already holds is skipped by its
own ordering rule. A Job that ended while the plan still needs load is replaced by the next sweep.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from .store import EventError
from .window_store import ProducerIdentity, WindowIntake

STATES = ("SHIFT", "CUTOVER", "WATCH")
SUBJECT = "platform-load"
LABEL = "bravo.dev/platform-load"
PREFIX = "BRAVO_WINDOW "


def job_name(plan_id, version):
    return "t49-load-" + hashlib.sha256(f"{plan_id}:{version}".encode()).hexdigest()[:20]


def job_manifest(plan, *, image, base_url, source_env, target_env, duration, write_rps, read_rps, namespace):
    name = job_name(plan["plan_id"], plan["version"])
    command = ["python", "-m", "bravo_loadgen.cli", "run", "--base-url", base_url, "--measurement-path", "edge",
               "--generator", "platform-job", "--plan-id", plan["plan_id"], "--source-env", source_env,
               "--target-env", target_env, "--duration", str(duration), "--out", "/dev/shm/out",
               "--run-id", name, "--write-rps", str(write_rps), "--read-rps", str(read_rps),
               "--window-stdout", "--fresh-connections"]
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": namespace, "labels": {LABEL: "true", "component": "platform"}},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": duration + 300, "ttlSecondsAfterFinished": 3600,
                     "template": {"metadata": {"labels": {LABEL: "true", "component": "platform"}},
                                  # The bravo-initial admission policy (initial-proposal.yaml) admits only this
                                  # shape: the reviewed Reader image (it carries bravo_loadgen), the initial-proposal
                                  # SA (its GHCR pull secret), no Secrets, and only a broker-token volume.
                                  "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                           "serviceAccountName": "initial-proposal",
                                           "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                               "runAsGroup": 10001,
                                                               "seccompProfile": {"type": "RuntimeDefault"}},
                                           "containers": [{"name": "load", "image": image, "command": command,
                                                           "securityContext": {"allowPrivilegeEscalation": False,
                                                                               "readOnlyRootFilesystem": True,
                                                                               "capabilities": {"drop": ["ALL"]}},
                                                           "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                                         "limits": {"cpu": "1", "memory": "256Mi"}}}],
                                           # ponytail: the policy's volume rule needs one volume; this token is never
                                           # mounted in the container. Output goes to /dev/shm (runtime tmpfs).
                                           "volumes": [{"name": "unused-token", "projected": {"sources": [
                                               {"serviceAccountToken": {"audience": "bravo-subscription-broker",
                                                                        "expirationSeconds": 600,
                                                                        "path": "token"}}]}}]}}}}


def windows_from_log(text):
    """The windows the Job printed, oldest first; any other log line is ignored."""
    found = []
    for line in text.splitlines():
        if line.startswith(PREFIX):
            try:
                found.append(json.loads(line[len(PREFIX):]))
            except ValueError:
                continue
    return found


class PlatformLoad:
    # 20 rps: 200 requests a 10 s window, about 20 on a 10 % pool (profile demo-shift-floor v1).
    def __init__(self, store, kubectl, delegations, *, image, base_url, source_env, target_env, duration=1200,
                 write_rps=5, read_rps=15, namespace="bravo-initial", clock=None):
        if not image.startswith("ghcr.io/softbank-hackathon-finals/platform-product-reader@sha256:"):
            raise ValueError("platform_load_image_invalid")  # the only image the bravo-initial policy admits
        self.store, self.kubectl, self.delegations = store, kubectl, delegations
        self.job = dict(image=image, base_url=base_url, source_env=source_env, target_env=target_env,
                        duration=duration, write_rps=write_rps, read_rps=read_rps, namespace=namespace)
        self.intake = WindowIntake(store, clock=clock)
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def sweep(self):
        with self.store._transaction() as c:
            plans = [dict(r) for r in c.execute(
                "SELECT plan_id, tenant_id, state, version, spec_json FROM plans WHERE state IN (?,?,?)", STATES).fetchall()]
            plans = [p | {"delegation": d} for p in plans
                     if (d := self.delegations.live(c, p["plan_id"])) and "shift" in d.get("gates", [])]
        done = {}
        for plan in plans:
            try:
                producer = self._bind(plan)
                job = self._ensure(plan)
                done[plan["plan_id"]] = {"job": job, "received": self._ingest(plan, producer, job)}
            except EventError as error:
                done[plan["plan_id"]] = {"held": error.code}
        return done

    def _bind(self, plan):
        """The platform producer, bound to the plan's current state and version (WindowIntake requires both)."""
        from .window_store import _binding

        expires = min(datetime.fromisoformat(self.clock()) + timedelta(hours=2),
                      datetime.fromisoformat(plan["delegation"]["expires_at"])).isoformat()
        producer = ProducerIdentity(SUBJECT, plan["tenant_id"], plan["plan_id"], f"{SUBJECT}-{plan['version']}", expires)
        with self.store._transaction() as c:
            current = _binding(c, plan["plan_id"], SUBJECT)
        if current and (current["plan_version"], current["plan_state"], current["credential_id"]) == (
                plan["version"], plan["state"], producer.credential_id) and current["enabled"]:
            return ProducerIdentity(SUBJECT, plan["tenant_id"], plan["plan_id"], current["credential_id"],
                                    current["expires_at"])
        self.intake.set_binding(producer, enabled=True, expected_revision=current["revision"] if current else 0,
                                administration_ref="delegation-" + plan["delegation"]["delegation_id"],
                                command_key=f"{SUBJECT}-{plan['version']}-{plan['state'].lower()}")
        return producer

    def _ensure(self, plan):
        name = job_name(plan["plan_id"], _anchor_version(plan))
        code, out, _ = self.kubectl(["get", "job", name])
        if code == 0:
            status = json.loads(out).get("status", {})
            if not (status.get("succeeded") or status.get("failed")):
                return name
        manifest = job_manifest(plan | {"version": _anchor_version(plan)}, **self.job)
        code, _, err = self.kubectl(["create", "-f", "-"], json.dumps(manifest).encode())
        if code != 0 and b"AlreadyExists" not in err:
            raise EventError("platform_load_unavailable")
        return name

    def _ingest(self, plan, producer, name):
        code, out, _ = self.kubectl(["logs", f"job/{name}", "--tail", "200"])
        if code != 0:
            return 0
        received = 0
        for window in windows_from_log(out.decode(errors="replace")):
            try:
                self.intake.receive(producer, window)
                received += 1
            except (EventError, ValueError):
                continue  # an older or duplicate window, or one T36 refuses: not received again
        return received


def _anchor_version(plan):
    """One Job spans SHIFT..WATCH: name it after the plan, not each version, so a state change keeps the load."""
    return "load"
