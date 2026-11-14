"""T14 operator run for one environment: the registered collectors of a profile → one ControlRun file.

    uv run --directory services/control-runner python ../../operations/controls/run.py \
        --registration /private/source.json --out /private/runs/<new dir>

The registration is a private file the operator keeps outside Git (never uploaded, never committed). It
names the app, the environment, the profile and, per test, which existing collector measures it and
against what. A test with no registration is not skipped: the Runner reports it UNKNOWN (NOT_RUN),
and the gate blocks on it. The adapter must belong to the environment's role, so a source registration
copied with only its role changed is refused, never measured. Credentials are never in the registration
or argv:

- source, AWS (`aws-object`, `aws-secret`): the operator's own AWS profile assumes each registered role
  for 15 minutes; `QualifiedAwsProbe` then checks the STS identity against the registered RoleId.
- target, S3-compatible endpoint (`s3-endpoint`, the target's SeaweedFS): the app's `client` identity from
  two environment variables the registration names (from the app's `target-app-objects` Secret; never
  `target-seaweedfs-auth`, which also holds the bootstrap admin keys). The operator's AWS_PROFILE and
  AWS_CA_BUNDLE are not read.
- either role, DB admin login audit (`pod-log`, AUD-01.1): when the Runner calls the collector, the
  operator's kubectl (registered context) logs in to the CNPG primary as `postgres` over the pod's local
  socket with `application_name=<marker>` and checks `rolsuper`, then searches that container's own log
  (`pods/log`) until the marker's `connection authorized` line appears or 3 minutes pass. No DB password
  is handled. Neither environment ships CNPG logs to a store yet (ADR-0037).
- either role, app -> pooler TLS (`pooler-tls`, ENC-01.1, apply-recheck:1): a qualified psql 18 (path, sha256,
  version) connects with sslmode=verify-full through a loopback port-forward to the registered pooler
  (`PGHOSTADDR` 127.0.0.1, `PGHOST` the name its certificate carries). The app's DB password is read from
  the variable `password_env` names (from the app's own credential Secret). A wrong CA, a wrong hostname and
  plaintext stay the collector's ENC-01.2/.3. The evidence records the client as `port-forward`.
- target, OpenBao (`openbao`): when the Runner calls the collector, each ServiceAccount's token file
  (`kubectl create token`, mode 0600) is exchanged for an OpenBao token through the registered Kubernetes
  auth mount; the accessor binds the identity. Both tokens are revoked (revoke-self) after the collector
  returns. A failed login makes SEC-01.1 UNKNOWN, never the other tests.

From the operator machine the target's in-cluster Services are reached through `kubectl port-forward`
(registration.example.json). SeaweedFS serves plain HTTP inside the cluster, so its endpoint is
`http://127.0.0.1:<port>`; OpenBao keeps TLS, so its origin is `https://127.0.0.1:<port>` and
`server_hostname` names the Service its certificate carries (SNI and hostname check):

    kubectl -n target-dev port-forward svc/seaweedfs 18333:8333
    kubectl -n bravo-secrets port-forward svc/target-openbao 18200:8200

Output (a new 0700 directory, an absolute path outside this repository): `control-run.json` (0600) and
one evidence file per collector. The printed `record` command records it for one plan through the gate
relay. Exit: 0 written (read the verdicts: UNKNOWN and FAIL still block), 1 failed (input, registration,
I/O).
"""

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# ADR-0034 both.control_probes: operator adapters by environment kind. Each test id here is one the
# kind's probe registry (bravo_controls.default_registry) supports; gcp has no operator adapter yet.
KIND_ADAPTERS = {"aws": {"IAM-01.1": {"aws-object"}, "SEC-01.1": {"aws-secret"}, "AUD-01.1": {"pod-log"},
                         "ENC-01.1": {"pooler-tls"}},
                 "onprem": {"IAM-01.1": {"s3-endpoint"}, "SEC-01.1": {"openbao"}, "AUD-01.1": {"pod-log"},
                            "ENC-01.1": {"pooler-tls"}},
                 "gcp": {}}
# The registration names a role; the aws->onprem demo fixes each role's kind.
ROLE_KIND = {"source": "aws", "target": "onprem"}
ADAPTERS = {role: KIND_ADAPTERS[kind] for role, kind in ROLE_KIND.items()}
# ENC-01.1 from outside the namespace: the client is the port-forward, recorded as such in the evidence.
POOLER_CLIENT = "port-forward"
SESSION_SECONDS = 900
CANARY_WINDOW = timedelta(minutes=3)
POLL_SECONDS = 3
ADMIN_CHECK = "SELECT current_user || '|' || rolsuper::text FROM pg_roles WHERE rolname = current_user"


def _output_dir(path):
    """A new directory at an absolute path outside the repository (evidence names roles and accounts)."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("out_must_be_absolute")
    resolved = path.parent.resolve() / path.name
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("out_inside_repository")
    return resolved


def _private_new_dir(path):
    path.mkdir(mode=0o700, parents=False, exist_ok=False)  # a new run never reuses an old directory
    return path


class Evidence:
    """Each collector's retained evidence as its own 0600 file in the run directory."""

    def __init__(self, directory):
        self.directory, self.count = Path(directory), 0

    def sink(self, label):
        def keep(data: bytes):
            self.count += 1
            fd = os.open(self.directory / f"evidence-{self.count:02d}-{label}.json",
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
        return keep


def assume_role_provider(profile, *, clock=lambda: datetime.now(UTC)):
    """role -> TemporaryCredentials, from the operator's own profile (STS AssumeRole, 15 minutes)."""
    import botocore.session
    from bravo_controls.aws_identity import TemporaryCredentials

    def provide(role):
        session = botocore.session.Session(profile=profile)
        sts = session.create_client("sts")
        try:
            answer = sts.assume_role(RoleArn=role.arn, RoleSessionName="bravo-t14-" + clock().strftime("%H%M%S"),
                                     DurationSeconds=SESSION_SECONDS)["Credentials"]
        finally:
            sts.close()
        return TemporaryCredentials(answer["AccessKeyId"], answer["SecretAccessKey"], answer["SessionToken"],
                                    answer["Expiration"])
    return provide


def _bao(origin, ca_pem, server_hostname, timeout):
    from urllib.parse import urlsplit

    from bravo_controls.openbao import check_server_name, tls_context

    url = urlsplit(origin)
    check_server_name(url.hostname, server_hostname)
    return http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=timeout,
                                       context=tls_context(ca_pem, server_hostname))


def openbao_login(origin, ca_pem, auth_mount, role, jwt, *, server_hostname=None, timeout=3.0):
    """One Kubernetes auth login: (client token, accessor). Nothing is retried or logged."""
    connection = _bao(origin, ca_pem, server_hostname, timeout)
    try:
        connection.request("POST", f"/v1/auth/{auth_mount}/login", body=json.dumps({"role": role, "jwt": jwt}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read(65536)
    finally:
        connection.close()
    if response.status != 200:
        raise PermissionError("openbao_login_refused")
    auth = json.loads(body).get("auth") or {}
    if not isinstance(auth.get("client_token"), str) or not isinstance(auth.get("accessor"), str):
        raise ValueError("openbao_login_invalid")
    return auth["client_token"], auth["accessor"]


def openbao_revoke(origin, ca_pem, token, *, server_hostname=None, timeout=3.0):
    """revoke-self; otherwise the token's own TTL bounds it."""
    connection = _bao(origin, ca_pem, server_hostname, timeout)
    try:
        connection.request("POST", "/v1/auth/token/revoke-self", headers={"X-Vault-Token": token})
        connection.getresponse().read(65536)
    finally:
        connection.close()


def _private_file(path, label):
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError(f"token_file_not_private:{label}")  # a ServiceAccount token is a credential
    return path


class OpenBaoRun:
    """SEC-01.1: log both ServiceAccounts in when the Runner calls it, observe, revoke both tokens."""

    def __init__(self, entry, target, evidence_sink, *, login, revoke, read):
        from urllib.parse import urlsplit

        from bravo_controls.openbao import check_server_name

        # Registration errors stop the run before any login.
        check_server_name(urlsplit(entry["origin"]).hostname, entry.get("server_hostname"))
        self.entry, self.target, self.sink = entry, target, evidence_sink
        self.login, self.revoke, self.read = login, revoke, read
        self.ca_pem = read(Path(entry["ca_file"]))
        self.jwt_files = {side: _private_file(Path(entry[side]["jwt_file"]), f"SEC-01.1:{side}")
                          for side in ("app", "other")}

    def __call__(self, context, spec):
        from bravo_controls.openbao import (
            KubernetesPrincipal,
            OpenBaoClient,
            OpenBaoSecretAccessProbe,
        )

        entry, name = self.entry, self.entry.get("server_hostname")
        clients = []
        try:
            for side in ("app", "other"):
                who = entry[side]
                token, accessor = self.login(entry["origin"], self.ca_pem, entry["auth_mount"], who["role"],
                                             self.read(self.jwt_files[side]).strip(), server_hostname=name)
                principal = KubernetesPrincipal(entry["auth_mount"], who["namespace"], who["service_account"],
                                                who["uid"], who["role"], accessor)
                clients.append(OpenBaoClient(entry["origin"], self.ca_pem, token, principal, server_hostname=name))
            probe = OpenBaoSecretAccessProbe(self.target, app_client=clients[0], other_client=clients[1],
                                             evidence_sink=self.sink)
            return probe(context, spec)
        finally:
            for client in clients:
                try:
                    self.revoke(entry["origin"], self.ca_pem, client.token, server_hostname=name)
                except Exception:  # noqa: BLE001 - the verdict stands; the token's TTL still bounds it
                    pass


def kubectl(context, *args, data=None, timeout=30):
    """The operator's kubectl on one registered context; output bytes, never printed."""
    import subprocess

    return subprocess.run(["kubectl", "--context", context, "--request-timeout", f"{timeout}s", *args],
                          input=data, capture_output=True, check=True, timeout=timeout + 5).stdout


class DbLoginAuditRun:
    """AUD-01.1: one admin login canary on the CNPG primary, then its own log is searched (ADR-0037)."""

    def __init__(self, entry, evidence_sink, *, kube=kubectl, clock=lambda: datetime.now(UTC), sleep=None):
        import time

        from bravo_controls.pod_log_audit import NAME

        fields = ("kube_context", "namespace", "cluster", "database")
        if any(not isinstance(entry.get(f), str) or not entry[f] for f in fields) or any(
                not NAME.fullmatch(entry[f]) for f in ("namespace", "cluster")):
            raise ValueError("registration_fields:AUD-01.1")
        self.entry, self.sink, self.kube, self.clock = entry, evidence_sink, kube, clock
        self.sleep = sleep or time.sleep
        self.container = entry.get("container", "postgres")

    def _get(self, path):
        return self.kube(self.entry["kube_context"], "get", "--raw", path)

    def __call__(self, context, spec):
        from uuid import uuid4

        from bravo_controls.audit_probes import AuditSearchProbe, CanaryReceipt
        from bravo_controls.pod_log_audit import PodLogAuditSearch, pod_path

        entry, namespace = self.entry, self.entry["namespace"]
        cluster = json.loads(self._get(f"/apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters/"
                                       f"{entry['cluster']}"))
        pod = cluster.get("status", {}).get("currentPrimary")
        if not isinstance(pod, str) or not pod:
            raise ValueError("cnpg_primary_unknown")
        uid = json.loads(self._get(pod_path(namespace, pod)))["metadata"]["uid"]
        marker = "bravo_audit_" + uuid4().hex
        started = self.clock()
        answer = self.kube(entry["kube_context"], "exec", "-i", "-n", namespace, pod, "-c", self.container, "--",
                           "psql", "-X", "-v", "ON_ERROR_STOP=1", "-At",
                           "-d", f"dbname={entry['database']} application_name={marker}", "-c", ADMIN_CHECK)
        if answer.decode().strip() != "postgres|true":
            raise ValueError("admin_login_not_confirmed")  # the canary itself did not happen as registered
        receipt = CanaryReceipt(context, "AUD-01.1", marker, "postgres", entry["database"], namespace, "public",
                                started, self.clock(), started + CANARY_WINDOW, frozenset({"login"}))
        binding = f"kubernetes-pod-log:{context.environment.ref}:{namespace}/{pod}/{self.container}:{uid}"
        search = PodLogAuditSearch(self._get, namespace=namespace, pod=pod, container=self.container,
                                   source_binding=binding, clock=self.clock)
        while True:
            kept = []
            probe = AuditSearchProbe(receipt_provider=lambda *_: receipt, search=search, source_binding=binding,
                                     streams=(search.stream,), evidence_sink=kept.append, clock=self.clock)
            observation = probe(context, spec)
            if observation.observed is not None or self.clock() >= receipt.deadline:
                break
            self.sleep(POLL_SECONDS)
        for data in kept:  # only the final search's evidence
            self.sink(data)
        return observation


POOLER_FIELDS = {"adapter", "namespace", "pooler_uid", "hostname", "address", "port", "database", "username",
                 "ca_file", "wrong_ca_file", "password_env", "psql", "psql_sha256", "psql_version"}


def pooler_tls_probe(app, environment, entry, sink, *, environ=os.environ):
    """ENC-01.1 (verify-full to the pooler) through a loopback port-forward to the registered pooler.

    TLS is end to end with the pooler, so this shows the pooler's own certificate chain, hostname and
    minimum version; it does not show the app namespace's network path. The app credential comes from
    the named variable only and never reaches argv, a file or the evidence.
    """
    from bravo_controls.postgres_tls import PoolerTarget, PoolerTLSProbe

    if type(entry) is not dict or set(entry) != POOLER_FIELDS:
        raise ValueError("pooler_tls_fields")
    if not ipaddress.ip_address(entry["address"]).is_loopback:
        raise ValueError("pooler_tls_requires_port_forward")
    password = environ.get(entry["password_env"])
    if not password:
        raise ValueError("credentials_missing:ENC-01.1")  # names only; values never printed
    target = PoolerTarget(app, environment, entry["namespace"], POOLER_CLIENT, entry["pooler_uid"],
                          "sha256:" + hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest(),
                          entry["hostname"], entry["address"], entry["port"], entry["database"],
                          entry["username"], Path(entry["ca_file"]).read_bytes(),
                          Path(entry["wrong_ca_file"]).read_bytes())
    return PoolerTLSProbe(target, executable=Path(entry["psql"]), executable_sha256=entry["psql_sha256"],
                          psql_version=entry["psql_version"], credential_provider=lambda _context: password,
                          evidence_sink=sink)


def build_probes(registration, environment, evidence, *, environ=os.environ, aws_provider=assume_role_provider,
                 bao_login=openbao_login, bao_revoke=openbao_revoke, read=Path.read_text, kube=kubectl):
    """test id -> collector, only for tests the registration names with an adapter of the environment's role."""
    from bravo_controls.aws_identity import RegisteredRole

    app, probes = registration["app"], {}
    for test_id, entry in sorted(registration.get("tests", {}).items()):
        adapter = entry.get("adapter") if isinstance(entry, dict) else None
        if adapter not in ADAPTERS.get(environment.role, {}).get(test_id, ()):
            raise ValueError(f"unknown_adapter:{test_id}")
        if adapter in ("aws-object", "aws-secret"):
            from bravo_controls.aws_probes import ObjectTarget, SecretTarget
            from bravo_controls.aws_qualification import QualifiedAwsProbe

            role = RegisteredRole(entry["app_role"]["arn"], entry["app_role"]["role_id"])
            if adapter == "aws-object":
                target = ObjectTarget(app, environment, entry["account_id"], entry["region"], entry["bucket"],
                                      entry["key"], entry["version_id"])
                other = None
            else:
                target = SecretTarget(app, environment, entry["secret_arn"], entry["version_id"])
                other = RegisteredRole(entry["other_role"]["arn"], entry["other_role"]["role_id"])
            probes[test_id] = QualifiedAwsProbe(target, app_role=role, other_role=other,
                                                credential_provider=aws_provider(entry["aws_profile"]),
                                                evidence_sink=evidence.sink(test_id))
        elif adapter == "s3-endpoint":
            from bravo_controls.object_endpoint import (
                EndpointAnonymousReadProbe,
                EndpointObjectTarget,
                endpoint_clients,
            )

            target = EndpointObjectTarget(app, environment, entry["endpoint"], entry["bucket"], entry["key"],
                                          entry.get("region", "us-east-1"))
            access, secret = environ.get(entry["access_key_env"]), environ.get(entry["secret_key_env"])
            if not access or not secret:
                raise ValueError(f"credentials_missing:{test_id}")  # names only; values never printed
            app_client, anonymous = endpoint_clients(target, access_key_id=access, secret_access_key=secret,
                                                     ca_bundle=entry.get("ca_file"))
            probes[test_id] = EndpointAnonymousReadProbe(target, app_client=app_client, anonymous_client=anonymous,
                                                         app_identity=entry.get("identity", "client"),
                                                         evidence_sink=evidence.sink(test_id))
        elif adapter == "pod-log":
            probes[test_id] = DbLoginAuditRun(entry, evidence.sink(test_id), kube=kube)
        elif adapter == "pooler-tls":
            probes[test_id] = pooler_tls_probe(app, environment, entry, evidence.sink(test_id), environ=environ)
        else:  # openbao
            from bravo_controls.openbao import BaoTarget

            target = BaoTarget(app, environment, entry["mount"], entry["path"], entry["version"])
            probes[test_id] = OpenBaoRun(entry, target, evidence.sink(test_id), login=bao_login,
                                         revoke=bao_revoke, read=read)
    return probes


def run(registration, out, *, build=None, clock=lambda: datetime.now(UTC)):
    from bravo_controls import Catalog, Environment, Runner

    if (type(registration) is not dict or not {"app", "environment", "profile", "tests"} <= set(registration)
            or set(registration) - {"app", "environment", "profile", "tests"}):
        raise ValueError("registration_fields")
    env = registration["environment"]
    environment = Environment(env["id"], env["role"], env["ref"])
    catalog = Catalog(json.loads((ROOT / "docs/controls/profiles.json").read_text()),
                      json.loads((ROOT / "docs/controls/control-result.schema.json").read_text()))
    directory = _output_dir(out)
    # Nothing is contacted or written yet.
    probes = (build or build_probes)(registration, environment, Evidence(directory))
    _private_new_dir(directory)
    document = Runner(catalog, probes, clock=clock, evidence_ttl=timedelta(hours=24)).run(
        profile_id=registration["profile"]["id"], profile_version=registration["profile"]["version"],
        app=registration["app"], environment=environment)
    fd = os.open(directory / "control-run.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return document, directory


def main(argv=None):
    from botocore.exceptions import BotoCoreError

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registration", required=True, help="private registration JSON (outside Git)")
    parser.add_argument("--out", required=True, help="new private directory (absolute, outside the repository)")
    args = parser.parse_args(argv)
    try:
        document, directory = run(json.loads(Path(args.registration).read_text()), args.out)
    except (ValueError, KeyError, OSError, http.client.HTTPException, BotoCoreError) as error:
        # A bounded code only: registration values, credentials, paths and SDK text never reach the terminal.
        code = error.args[0] if isinstance(error, ValueError) and error.args and isinstance(error.args[0], str) else ""
        print(json.dumps({"status": "FAILED", "reason": type(error).__name__ + (":" + code[:80] if code else "")}))
        return 1
    verdicts = {row["test_id"]: row["verdict"] for row in document["results"]}
    print(json.dumps({"status": "WRITTEN", "run": str(directory / "control-run.json"), "verdicts": verdicts,
                      "record": "uv run --directory services/control-gate python ../../operations/gate/run.py "
                                "record --context <platform kube context> --plan-id <plan id> "
                                f"--side {document['environment']['role']} --run {directory / 'control-run.json'}"},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
