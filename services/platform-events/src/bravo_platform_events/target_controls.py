"""T14 target run after the cutover, collected by the platform for the WATCH retest (ADR-0041).

watch_retest compares the GATE source run with a target run that started after the cutover. This is
its producer, run by the workflow host outside the plan lock, at most once per plan and attempt window:

- The platform's own short-lived target identity (one kubeconfig, no other secret) does what the
  operator tool (operations/controls/run.py) does from a Mac: it mints the two ServiceAccount tokens
  SEC-01.1 compares from *outside* the cluster with a fixed audience and lifetime, reads the app's
  object client keys, and reaches the in-cluster services through API port-forwards. Nothing in the
  cluster can mint tokens (#666 review). Tokens and keys stay in memory or a 0700 temp directory.
- The registered T14 collectors (bravo_controls) measure; the Runner builds one ControlRun for the
  GATE context's own profile. A test with no registered adapter is NOT_RUN, so the retest stays
  UNKNOWN rather than passing (no placeholder result).
- The run is recorded as the plan's target side (GateRecords.record_run, gate:record grant).

ports.json "watch": {..., "target_controls": "/workflow/target-controls.json"} with
    {"kubeconfig": "/t14/kubeconfig", "evidence_dir": "/workflow-state/t14-evidence",
     "environment_ref": "<target connection ref>",
     "tests": {"SEC-01.1": {"adapter": "openbao", ...}, "IAM-01.1": {"adapter": "s3-endpoint", ...}}}
"""

import base64
import contextlib
import json
import logging
import os
import re
import select
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)
TOKEN_AUDIENCE = "openbao"
TOKEN_SECONDS = 600
RETRY_SECONDS = 300  # ponytail: per-process attempt window; a stored attempt record if hosts multiply
ADAPTERS = {"SEC-01.1": "openbao", "IAM-01.1": "s3-endpoint"}  # ENC-01.1 pooler-tls: not wired yet
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


class Kube:
    """kubectl on the platform's target identity. Output bytes are never logged."""

    def __init__(self, kubeconfig, *, binary="kubectl", timeout=20):
        self.kubeconfig, self.binary, self.timeout = str(kubeconfig), binary, timeout

    def _args(self, *args):
        return [self.binary, "--kubeconfig", self.kubeconfig, "--request-timeout", f"{self.timeout}s", *args]

    def _run(self, *args):
        result = subprocess.run(self._args(*args), capture_output=True, timeout=self.timeout + 10, check=False)
        if result.returncode:
            raise RuntimeError("target_api_refused:" + args[0])  # stderr may name objects; not kept
        return result.stdout

    def token(self, namespace, service_account):
        return self._run("create", "token", service_account, "-n", namespace, "--audience", TOKEN_AUDIENCE,
                         "--duration", f"{TOKEN_SECONDS}s").decode().strip()

    def secret(self, namespace, name, key):
        raw = self._run("get", "secret", name, "-n", namespace, "-o", "jsonpath={.data." + key + "}")
        return base64.b64decode(raw).decode()

    @contextlib.contextmanager
    def forward(self, namespace, service, port):
        """A local port to svc/<service>:<port> through the target API for the duration of the block."""
        process = subprocess.Popen(self._args("port-forward", "-n", namespace, f"svc/{service}", f":{port}",
                                              "--address", "127.0.0.1"),
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            ready, _, _ = select.select([process.stdout], [], [], self.timeout)
            line = process.stdout.readline() if ready else ""
            match = re.search(r"127\.0\.0\.1:(\d+) ->", line)
            if match is None:
                raise RuntimeError("port_forward_failed:" + service)
            yield int(match.group(1))
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(5)


def _bao_connection(origin, ca_pem, server_hostname):
    import http.client
    from urllib.parse import urlsplit

    from bravo_controls.openbao import check_server_name, tls_context

    url = urlsplit(origin)
    check_server_name(url.hostname, server_hostname)
    return http.client.HTTPSConnection(url.hostname, url.port, timeout=3.0,
                                       context=tls_context(ca_pem, server_hostname))


def _bao_login(origin, ca_pem, server_hostname, auth_mount, role, jwt):
    connection = _bao_connection(origin, ca_pem, server_hostname)
    try:
        connection.request("POST", f"/v1/auth/{auth_mount}/login", body=json.dumps({"role": role, "jwt": jwt}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read(65536)
    finally:
        connection.close()
    auth = (json.loads(body).get("auth") or {}) if response.status == 200 else {}
    if not isinstance(auth.get("client_token"), str) or not isinstance(auth.get("accessor"), str):
        raise PermissionError("openbao_login_refused")
    return auth["client_token"], auth["accessor"]


def _bao_revoke(origin, ca_pem, server_hostname, token):
    connection = _bao_connection(origin, ca_pem, server_hostname)
    try:
        connection.request("POST", "/v1/auth/token/revoke-self", headers={"X-Vault-Token": token})
        connection.getresponse().read(65536)
    finally:
        connection.close()


class _OpenBaoRun:
    """SEC-01.1: both identities log in when the Runner calls it; both tokens are revoked after."""

    def __init__(self, entry, target, origin, ca_pem, jwts, sink):
        self.entry, self.target, self.origin, self.ca_pem, self.jwts, self.sink = entry, target, origin, ca_pem, jwts, sink

    def __call__(self, context, spec):
        from bravo_controls.openbao import (
            KubernetesPrincipal,
            OpenBaoClient,
            OpenBaoSecretAccessProbe,
        )

        entry, name, clients = self.entry, self.entry["server_hostname"], []
        try:
            for side in ("app", "other"):
                who = entry[side]
                token, accessor = _bao_login(self.origin, self.ca_pem, name, entry["auth_mount"], who["role"],
                                             self.jwts[side])
                principal = KubernetesPrincipal(entry["auth_mount"], who["namespace"], who["service_account"],
                                                who["uid"], who["role"], accessor)
                clients.append(OpenBaoClient(self.origin, self.ca_pem, token, principal, server_hostname=name))
            return OpenBaoSecretAccessProbe(self.target, app_client=clients[0], other_client=clients[1],
                                            evidence_sink=self.sink)(context, spec)
        finally:
            for client in clients:
                with contextlib.suppress(Exception):  # the verdict stands; the token TTL still bounds it
                    _bao_revoke(self.origin, self.ca_pem, name, client.token)


def _probe(test_id, entry, *, kube, stack, app, environment, sink):
    if ADAPTERS.get(test_id) != entry.get("adapter"):
        raise ValueError(f"unknown_adapter:{test_id}")
    port = stack.enter_context(kube.forward(entry["namespace"], entry["service"], entry["port"]))
    if entry["adapter"] == "openbao":
        from bravo_controls.openbao import BaoTarget

        jwts = {side: kube.token(entry[side]["namespace"], entry[side]["service_account"]) for side in ("app", "other")}
        target = BaoTarget(app, environment, entry["mount"], entry["path"], entry["version"])
        ca_pem = Path(entry["ca_file"]).read_text()
        return _OpenBaoRun(entry, target, f"https://127.0.0.1:{port}", ca_pem, jwts, sink)
    from bravo_controls.object_endpoint import (
        EndpointAnonymousReadProbe,
        EndpointObjectTarget,
        endpoint_clients,
    )

    target = EndpointObjectTarget(app, environment, f"http://127.0.0.1:{port}", entry["bucket"], entry["key"],
                                  entry.get("region", "us-east-1"))
    keys = [kube.secret(entry["namespace"], entry["credentials_secret"], k)
            for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")]
    app_client, anonymous = endpoint_clients(target, access_key_id=keys[0], secret_access_key=keys[1])
    return EndpointAnonymousReadProbe(target, app_client=app_client, anonymous_client=anonymous,
                                      app_identity=entry.get("identity", "client"), evidence_sink=sink)


def collect(registration, kube, *, profile, app, environment_id, clock=lambda: datetime.now(UTC)):
    """One ControlRun for `profile` on the target, measured now with the registered collectors."""
    from bravo_controls import Catalog, Environment, Runner
    from bravo_gate.contract import T5Contract

    contract = T5Contract()
    environment = Environment(environment_id, "target", registration["environment_ref"])
    evidence = Path(registration["evidence_dir"]) / environment_id / clock().strftime("%Y%m%dT%H%M%S%fZ")
    evidence.mkdir(mode=0o700, parents=True)

    def sink(test_id):
        def write(raw):
            fd = os.open(evidence / f"{test_id}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
        return write

    with contextlib.ExitStack() as stack:
        probes = {test_id: _probe(test_id, entry, kube=kube, stack=stack, app=app, environment=environment,
                                  sink=sink(test_id))
                  for test_id, entry in sorted(registration["tests"].items())}
        return Runner(Catalog(contract.catalog, contract.schema), probes, clock=clock,
                      evidence_ttl=timedelta(hours=24)).run(profile_id=profile[0], profile_version=profile[1],
                                                           app=app, environment=environment)


def _locate(c, plan_id):
    """(GATE context, spec, the cutover's last observed update) for a WATCH plan; None otherwise."""
    from bravo_gate.contract import time as gate_time
    from bravo_gate.models import GateContext

    from .cutover import latest_observation
    from .failback import cutover_checkpoint
    from .gate_records import _current

    plan = c.execute("SELECT state,spec_json FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    gate = _current(c, plan_id) if plan and plan["state"] == "WATCH" else None
    if gate is None:
        return None
    command_id = cutover_checkpoint(c, plan_id)[0]["command_id"]
    finished = gate_time(latest_observation(c, plan_id, command_id)["snapshot"]["updated_at"])
    return GateContext(**gate["context"]), json.loads(plan["spec_json"]), finished


def _recorded_after(c, plan_id, environment_id, finished):
    from bravo_gate.contract import time as gate_time

    from .watch_retest import _runs

    return any(r.get("side") == "target" and "ref" in r and r["document"]["environment"]["id"] == environment_id
               and gate_time(r["document"]["started_at"]) >= finished for r in _runs(c, plan_id))


class TargetRunProducer:
    def __init__(self, store, records, principal, *, measure, locate=_locate, monotonic=time.monotonic):
        self.store, self.records, self.principal, self.measure = store, records, principal, measure
        self.locate, self.monotonic, self._tried = locate, monotonic, {}

    def ensure(self, plan_id, tenant_id):
        """Record one target run after the cutover unless one exists. Returns its ref, or None."""
        from bravo_gate.contract import canonical
        from bravo_gate.models import RunSnapshot

        with self.store._transaction() as c:
            located = self.locate(c, plan_id)
            if located is None:
                return None
            context, spec, finished = located
            if _recorded_after(c, plan_id, context.target_environment_id, finished):
                return None
        last = self._tried.get(plan_id)
        if last is not None and self.monotonic() - last < RETRY_SECONDS:
            return None
        self._tried[plan_id] = self.monotonic()
        document = self.measure(profile=(context.profile_id, context.profile_version), app=spec["workload_id"],
                                environment_id=context.target_environment_id)
        snapshot = RunSnapshot(document["run_id"], canonical(document))
        return self.records.record_run(self.principal(tenant_id), plan_id, "target", snapshot)["ref"]


def watch_preparer(producer, inner):
    """WATCH preparer: produce the post-cutover target run first, then the existing WATCH preparer."""
    def prepare(plan_id, tenant_id, version):
        try:
            producer.ensure(plan_id, tenant_id)
        except Exception:  # noqa: BLE001 - WATCH keeps holding on UNKNOWN; the next window retries
            log.exception("target control run not recorded", extra={"plan_id": plan_id})
        return inner(plan_id, tenant_id, version)
    return prepare


def create(store, config_file, *, subject="workflow-host"):
    """ports.json watch.target_controls -> TargetRunProducer over the platform's target identity."""
    from .gate_services import gate_records
    from .operator_access import OperatorAccess
    from .workflow_stages import host_principal

    registration = json.loads(Path(config_file).read_text())
    if (type(registration) is not dict or set(registration) != {"kubeconfig", "evidence_dir", "environment_ref", "tests"}
            or not registration["tests"] or set(registration["tests"]) - set(ADAPTERS)):
        raise ValueError("target_controls_configuration_invalid")
    kube = Kube(registration["kubeconfig"])
    records = gate_records(store, OperatorAccess(store))

    def measure(**kw):
        return collect(registration, kube, **kw)

    return TargetRunProducer(store, records, lambda tenant: host_principal(subject, tenant), measure=measure)
