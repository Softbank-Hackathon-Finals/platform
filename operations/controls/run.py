"""T14 operator run for one environment: the registered collectors of a profile → one ControlRun file.

    uv run --directory services/control-runner python ../../operations/controls/run.py \
        --registration /private/source.json --out /private/runs/<new dir>

The registration is a private file the operator keeps outside Git (never uploaded, never committed). It
names the app, the environment, the profile and, per test, which existing collector measures it and
against what. A test with no registration is not skipped: the Runner reports it UNKNOWN (NOT_RUN),
and the gate blocks on it. Credentials are never in the registration or argv:

- AWS (`aws-object`, `aws-secret`): the operator's own AWS profile assumes each registered role for 15
  minutes; `QualifiedAwsProbe` then checks the STS identity against the registered RoleId.
- S3-compatible endpoint (`s3-endpoint`, the target's SeaweedFS): the app's `client` identity from two
  environment variables the registration names (in a target Job, from the app's `target-app-objects` Secret;
  never `target-seaweedfs-auth`, which also holds the bootstrap admin keys).
- OpenBao (`openbao`): each ServiceAccount's projected token file is exchanged once for an OpenBao token
  through the registered Kubernetes auth mount; the accessor binds the identity.

Output (new 0700 directory): `control-run.json` (0600) and one evidence file per collector. Record it with
`operations/gate/run.py record --side <role> --run <dir>/control-run.json`.
Exit: 0 written (read the verdicts: UNKNOWN and FAIL still block), 1 failed (input, registration, I/O).
"""

import argparse
import json
import os
import ssl
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = {"IAM-01.1": {"aws-object", "s3-endpoint"}, "SEC-01.1": {"aws-secret", "openbao"}}
SESSION_SECONDS = 900


def _private_new_dir(path):
    path = Path(path)
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


def openbao_login(origin, ca_pem, auth_mount, role, jwt, *, timeout=3.0):
    """One Kubernetes auth login: (client token, accessor). Nothing is retried or logged."""
    import http.client
    from urllib.parse import urlsplit

    url = urlsplit(origin)
    context = ssl.create_default_context(cadata=ca_pem)
    connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=timeout, context=context)
    try:
        connection.request("POST", f"/v1/auth/{auth_mount}/login", body=json.dumps({"role": role, "jwt": jwt}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read(65536)
    finally:
        connection.close()
    if response.status != 200:
        raise ValueError("openbao_login_refused")
    auth = json.loads(body).get("auth") or {}
    if not isinstance(auth.get("client_token"), str) or not isinstance(auth.get("accessor"), str):
        raise ValueError("openbao_login_invalid")
    return auth["client_token"], auth["accessor"]


def build_probes(registration, environment, evidence, *, environ=os.environ, aws_provider=assume_role_provider,
                 bao_login=openbao_login, read=Path.read_text):
    """test id -> collector, only for tests the registration names with a known adapter."""
    from bravo_controls.aws_identity import RegisteredRole

    app, probes = registration["app"], {}
    for test_id, entry in sorted(registration.get("tests", {}).items()):
        adapter = entry.get("adapter") if isinstance(entry, dict) else None
        if adapter not in ADAPTERS.get(test_id, ()):
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
        else:  # openbao
            from bravo_controls.openbao import (
                BaoTarget,
                KubernetesPrincipal,
                OpenBaoClient,
                OpenBaoSecretAccessProbe,
            )

            ca_pem = read(Path(entry["ca_file"]))
            clients = []
            for side in ("app", "other"):
                who = entry[side]
                token, accessor = bao_login(entry["origin"], ca_pem, entry["auth_mount"], who["role"],
                                            read(Path(who["jwt_file"])).strip())
                principal = KubernetesPrincipal(entry["auth_mount"], who["namespace"], who["service_account"],
                                                who["uid"], who["role"], accessor)
                clients.append(OpenBaoClient(entry["origin"], ca_pem, token, principal))
            target = BaoTarget(app, environment, entry["mount"], entry["path"], entry["version"])
            probes[test_id] = OpenBaoSecretAccessProbe(target, app_client=clients[0], other_client=clients[1],
                                                       evidence_sink=evidence.sink(test_id))
    return probes


def run(registration, out, *, build=build_probes, clock=lambda: datetime.now(UTC)):
    from bravo_controls import Catalog, Environment, Runner

    if (type(registration) is not dict or not {"app", "environment", "profile", "tests"} <= set(registration)
            or set(registration) - {"app", "environment", "profile", "tests"}):
        raise ValueError("registration_fields")
    env = registration["environment"]
    environment = Environment(env["id"], env["role"], env["ref"])
    catalog = Catalog(json.loads((ROOT / "docs/controls/profiles.json").read_text()),
                      json.loads((ROOT / "docs/controls/control-result.schema.json").read_text()))
    directory = _private_new_dir(out)
    probes = build(registration, environment, Evidence(directory))
    document = Runner(catalog, probes, clock=clock, evidence_ttl=timedelta(hours=24)).run(
        profile_id=registration["profile"]["id"], profile_version=registration["profile"]["version"],
        app=registration["app"], environment=environment)
    fd = os.open(directory / "control-run.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return document, directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registration", required=True, help="private registration JSON (outside Git)")
    parser.add_argument("--out", required=True, help="new private directory for the run and its evidence")
    args = parser.parse_args(argv)
    try:
        document, directory = run(json.loads(Path(args.registration).read_text()), args.out)
    except (ValueError, KeyError, OSError) as error:
        # A bounded code only: registration values, credentials and SDK text never reach the terminal.
        code = error.args[0] if isinstance(error, ValueError) and error.args and isinstance(error.args[0], str) else ""
        print(json.dumps({"status": "FAILED", "reason": type(error).__name__ + (":" + code[:80] if code else "")}))
        return 1
    verdicts = {row["test_id"]: row["verdict"] for row in document["results"]}
    print(json.dumps({"status": "WRITTEN", "run": str(directory / "control-run.json"), "verdicts": verdicts,
                      "record": f"operations/gate/run.py record --side {document['environment']['role']} "
                                f"--run {directory / 'control-run.json'}"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
