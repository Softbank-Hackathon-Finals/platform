"""operations/controls/run.py: one environment's registered collectors → one ControlRun file."""

import importlib.util
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_openbao import identity, secret

from bravo_controls import Environment, Observation
from bravo_controls.aws_qualification import QualifiedAwsProbe
from bravo_controls.object_endpoint import EndpointAnonymousReadProbe
from bravo_controls.openbao import OpenBaoClient

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("controls_run", ROOT / "operations/controls/run.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

CA = (Path(__file__).parent / "fixtures/tls/ca.pem").read_text()
ROLE = {"arn": "arn:aws:iam::123456789012:role/demo-chat-app", "role_id": "AROAABCDEFGHIJKLMNOPQ"}
OTHER = {"arn": "arn:aws:iam::123456789012:role/demo-chat-other", "role_id": "AROAQPONMLKJIHGFEDCBA"}


def registration(tests, role="target"):
    return {"app": "demo-chat", "environment": {"id": f"env-{role}", "role": role, "ref": f"ref:{role}"},
            "profile": {"id": "demo-gate", "version": "1"}, "tests": tests}


def passing(context, spec):
    return Observation(context, spec.test_id, "1", "deny", "pass", datetime.now(UTC), "fixture", "1",
                       "sha256:" + "a" * 64)


def test_a_run_writes_one_private_control_run_and_unregistered_tests_stay_unknown(tmp_path):
    out = tmp_path / "run-1"
    document, directory = tool.run(registration({}), out, build=lambda *_: {"IAM-01.1": passing})
    verdicts = {row["test_id"]: row["verdict"] for row in document["results"]}
    assert verdicts == {"IAM-01.1": "PASS", "SEC-01.1": "UNKNOWN", "AUD-01.1": "UNKNOWN"}
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(directory / "control-run.json").st_mode) == 0o600
    assert json.loads((directory / "control-run.json").read_text()) == document
    with pytest.raises(FileExistsError):  # a new run never reuses an old directory
        tool.run(registration({}), out, build=lambda *_: {})


def test_main_prints_the_verdicts_and_the_record_command(tmp_path, capsys, monkeypatch):
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(registration({}, role="source")))
    monkeypatch.setattr(tool, "build_probes", lambda *_: {})
    assert tool.main(["--registration", str(path), "--out", str(tmp_path / "run")]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdicts"] == {"IAM-01.1": "UNKNOWN", "SEC-01.1": "UNKNOWN", "AUD-01.1": "UNKNOWN"}
    assert printed["record"].startswith("uv run --directory services/control-gate python ../../operations/gate/run.py "
                                        "record --context <platform kube context> --plan-id <plan id> --side source --run /")


def test_a_failure_before_the_run_prints_one_code_and_leaves_no_directory(tmp_path, capsys, monkeypatch):
    from botocore.exceptions import ProfileNotFound

    path = tmp_path / "registration.json"
    path.write_text(json.dumps(registration({})))

    def missing_profile(*_):
        raise ProfileNotFound(profile="operator-sso")

    monkeypatch.setattr(tool, "build_probes", missing_profile)
    assert tool.main(["--registration", str(path), "--out", str(tmp_path / "run")]) == 1
    printed = capsys.readouterr().out
    assert json.loads(printed) == {"status": "FAILED", "reason": "ProfileNotFound"} and "operator-sso" not in printed
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(("out", "code"), [
    ("runs/x", "out_must_be_absolute"),
    (str(ROOT / "runs" / "x"), "out_inside_repository"),
    (str(ROOT / "services" / ".." / "x"), "out_inside_repository"),
])
def test_the_output_is_an_absolute_path_outside_the_repository(out, code):
    with pytest.raises(ValueError, match=code):
        tool.run(registration({}), out, build=lambda *_: pytest.fail("nothing is built"))
    assert not (ROOT / "runs").exists() and not (ROOT / "x").exists()


@pytest.mark.parametrize(("role", "tests", "code"), [
    ("target", {"AUD-01.1": {"adapter": "cloudwatch"}}, "unknown_adapter:AUD-01.1"),  # not wired: refused
    ("target", {"AUD-01.2": {"adapter": "pod-log"}}, "unknown_adapter:AUD-01.2"),  # login only
    ("target", {"IAM-01.1": {"adapter": "aws-secret"}}, "unknown_adapter:IAM-01.1"),
    ("target", {"IAM-01.1": "s3-endpoint"}, "unknown_adapter:IAM-01.1"),
    # A source registration copied with only its role changed measures the source: refused.
    ("target", {"IAM-01.1": {"adapter": "aws-object"}}, "unknown_adapter:IAM-01.1"),
    ("target", {"SEC-01.1": {"adapter": "aws-secret"}}, "unknown_adapter:SEC-01.1"),
    ("source", {"IAM-01.1": {"adapter": "s3-endpoint"}}, "unknown_adapter:IAM-01.1"),
    ("source", {"SEC-01.1": {"adapter": "openbao"}}, "unknown_adapter:SEC-01.1"),
])
def test_an_adapter_outside_the_environments_role_is_refused(tmp_path, role, tests, code):
    env = Environment(f"env-{role}", role, f"ref:{role}")
    with pytest.raises(ValueError, match=code):
        tool.build_probes(registration(tests, role=role), env, tool.Evidence(tmp_path))


def test_the_target_object_adapter_takes_its_credentials_from_named_variables_only(tmp_path, capsys):
    env = Environment("env-target", "target", "ref:target")
    entry = {"adapter": "s3-endpoint", "endpoint": "https://objects.target.example:8333",
             "bucket": "demo-chat-attachments", "key": "attachments/canary.txt",
             "access_key_env": "T14_CLIENT_ACCESS", "secret_key_env": "T14_CLIENT_SECRET"}
    with pytest.raises(ValueError, match="credentials_missing:IAM-01.1"):
        tool.build_probes(registration({"IAM-01.1": entry}), env, tool.Evidence(tmp_path), environ={})
    probes = tool.build_probes(registration({"IAM-01.1": entry}), env, tool.Evidence(tmp_path),
                               environ={"T14_CLIENT_ACCESS": "client-access", "T14_CLIENT_SECRET": "client-secret"})
    assert isinstance(probes["IAM-01.1"], EndpointAnonymousReadProbe)
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(registration({"IAM-01.1": entry})))
    assert tool.main(["--registration", str(path), "--out", str(tmp_path / "run")]) == 1
    assert "credentials_missing" in capsys.readouterr().out


def test_the_aws_adapters_use_the_operator_profile_and_registered_roles(tmp_path):
    profiles = []
    env = Environment("env-source", "source", "ref:source")
    tests = {"IAM-01.1": {"adapter": "aws-object", "account_id": "123456789012", "region": "ap-northeast-2",
                          "bucket": "demo-chat-attachments", "key": "attachments/canary.txt",
                          "version_id": "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY", "app_role": ROLE, "aws_profile": "operator"},
             "SEC-01.1": {"adapter": "aws-secret",
                          "secret_arn": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:demo-chat/db-AbCdEf",
                          "version_id": "a" * 36, "app_role": ROLE, "other_role": OTHER, "aws_profile": "operator"}}
    probes = tool.build_probes(registration(tests, role="source"), env, tool.Evidence(tmp_path),
                               aws_provider=lambda profile: profiles.append(profile) or (lambda role: None))
    assert {k: type(v) for k, v in probes.items()} == {"IAM-01.1": QualifiedAwsProbe, "SEC-01.1": QualifiedAwsProbe}
    assert profiles == ["operator", "operator"]  # nothing contacted AWS yet: credentials come at run time


def bao_entry(tmp_path, mode=0o600, **extra):
    (tmp_path / "ca.pem").write_text(CA)
    for name in ("app", "other"):
        (tmp_path / f"{name}.jwt").write_text(f"jwt-{name}\n")
        (tmp_path / f"{name}.jwt").chmod(mode)
    who = lambda name: {"namespace": "target-dev", "service_account": name, "uid": f"uid-{name}",  # noqa: E731
                        "role": f"{name}-role", "jwt_file": str(tmp_path / f"{name}.jwt")}
    return {"adapter": "openbao", "origin": "https://127.0.0.1:18200", "ca_file": str(tmp_path / "ca.pem"),
            "server_hostname": "target-openbao.bravo-secrets.svc", "auth_mount": "kubernetes", "mount": "secret",
            "path": "demo-chat/db", "version": 3, "app": who("app"), "other": who("other")} | extra


class Bao:
    """The OpenBao an operator run talks to: Kubernetes logins, lookup-self, one KV read, revoke-self."""

    def __init__(self, monkeypatch, refuse=()):
        self.logins, self.revoked, self.refuse = [], [], refuse

        def get(client, path):
            if path.endswith("lookup-self"):
                return 200, identity(client.principal)
            if client.principal.service_account == "app":
                return 200, secret()
            return 403, {"errors": ["permission denied"]}

        monkeypatch.setattr(OpenBaoClient, "_get", get)

    def login(self, origin, ca_pem, mount, role, jwt, *, server_hostname):
        self.logins.append((origin, server_hostname, mount, role, jwt))
        if role in self.refuse:
            raise PermissionError("openbao_login_refused")
        return f"token-{role}", f"accessor-{role}"

    def revoke(self, origin, ca_pem, token, *, server_hostname):
        self.revoked.append(token)


def run_with_bao(tmp_path, bao, tests, extra=None):
    def build(registration_, environment, evidence):
        probes = tool.build_probes(registration_, environment, evidence, bao_login=bao.login,
                                   bao_revoke=bao.revoke)
        return probes | (extra or {})
    return tool.run(registration(tests), tmp_path / "run", build=build)


def test_the_openbao_adapter_logs_in_when_called_binds_the_accessor_and_revokes_after(tmp_path, monkeypatch):
    bao = Bao(monkeypatch)
    entry = bao_entry(tmp_path)
    tool.build_probes(registration({"SEC-01.1": entry}), Environment("env-target", "target", "ref:target"),
                      tool.Evidence(tmp_path), bao_login=bao.login, bao_revoke=bao.revoke)
    assert bao.logins == []  # building contacts nothing
    document, _ = run_with_bao(tmp_path, bao, {"SEC-01.1": entry})
    assert {r["test_id"]: r["verdict"] for r in document["results"]}["SEC-01.1"] == "PASS"
    assert bao.logins == [
        ("https://127.0.0.1:18200", "target-openbao.bravo-secrets.svc", "kubernetes", "app-role", "jwt-app"),
        ("https://127.0.0.1:18200", "target-openbao.bravo-secrets.svc", "kubernetes", "other-role", "jwt-other")]
    assert bao.revoked == ["token-app-role", "token-other-role"]


def test_a_refused_login_makes_only_sec_unknown_and_revokes_what_was_issued(tmp_path, monkeypatch):
    bao = Bao(monkeypatch, refuse={"other-role"})
    document, _ = run_with_bao(tmp_path, bao, {"SEC-01.1": bao_entry(tmp_path)}, extra={"IAM-01.1": passing})
    rows = {r["test_id"]: r for r in document["results"]}
    assert rows["IAM-01.1"]["verdict"] == "PASS"
    assert rows["SEC-01.1"]["verdict"] == "UNKNOWN" and rows["SEC-01.1"]["unknown_reason"] == "OBSERVATION_DENIED"
    assert bao.revoked == ["token-app-role"]


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_a_service_account_token_file_must_be_private(tmp_path, monkeypatch, mode):
    bao = Bao(monkeypatch)
    with pytest.raises(ValueError, match="token_file_not_private:SEC-01.1:app"):
        tool.build_probes(registration({"SEC-01.1": bao_entry(tmp_path, mode)}),
                          Environment("env-target", "target", "ref:target"), tool.Evidence(tmp_path),
                          bao_login=bao.login, bao_revoke=bao.revoke)


@pytest.mark.parametrize("extra", [{"server_hostname": "localhost", "origin": "https://bao.example.invalid:8200"},
                                   {"server_hostname": "Not A Name"}])
def test_a_server_name_is_only_for_a_loopback_port_forward(tmp_path, monkeypatch, extra):
    bao = Bao(monkeypatch)
    with pytest.raises(ValueError, match="server name"):
        tool.build_probes(registration({"SEC-01.1": bao_entry(tmp_path, **extra)}),
                          Environment("env-target", "target", "ref:target"), tool.Evidence(tmp_path),
                          bao_login=bao.login, bao_revoke=bao.revoke)
    with pytest.raises(ValueError, match="server name"):  # the login itself checks before connecting
        tool.openbao_login(extra.get("origin", "https://127.0.0.1:18200"), CA, "kubernetes", "r", "jwt",
                           server_hostname=extra["server_hostname"])


AUD = {"adapter": "pod-log", "kube_context": "target-cluster", "namespace": "target-dev", "cluster": "target-db",
       "database": "chat"}


class Kube:
    """The operator's kubectl against one CNPG cluster: the admin canary is logged by the primary."""

    def __init__(self, answer=b"postgres|true\n", logged_after=0, user="postgres"):
        self.answer, self.logged_after, self.user = answer, logged_after, user
        self.calls, self.lines, self.reads = [], [], 0

    def __call__(self, context, *args, data=None):
        self.calls.append((context, args))
        if args[0] == "exec":
            conninfo = args[args.index("-d") + 1]
            marker = conninfo.split("application_name=")[1]
            at = datetime.now(UTC)
            record = {"user_name": self.user, "database_name": "chat", "error_severity": "LOG",
                      "message": f"connection authorized: user={self.user} database=chat application_name={marker}"}
            self.lines.append(at.strftime("%Y-%m-%dT%H:%M:%S.%fZ") + " "
                              + json.dumps({"logger": "postgres", "msg": "record", "record": record}))
            return self.answer
        path = args[2]
        if "/clusters/" in path:
            return json.dumps({"status": {"currentPrimary": "target-db-1"}}).encode()
        if "/log?" in path:
            self.reads += 1
            return "\n".join(self.lines if self.reads > self.logged_after else []).encode()
        return json.dumps({"metadata": {"uid": "uid-1"}, "status": {"containerStatuses": [
            {"name": "postgres", "restartCount": 0, "state": {"running": {"startedAt": "2026-11-01T00:00:00Z"}}}]}}).encode()


def test_the_admin_login_canary_is_found_in_the_primarys_log_without_a_db_password(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "POLL_SECONDS", 0)
    kube = Kube(logged_after=2)  # the line shows up on the third read
    document, directory = tool.run(registration({"AUD-01.1": AUD}), tmp_path / "run",
                                   build=lambda r, e, ev: tool.build_probes(r, e, ev, kube=kube))
    assert {r["test_id"]: r["verdict"] for r in document["results"]}["AUD-01.1"] == "PASS"
    [psql] = [a for _, a in kube.calls if a[0] == "exec"]
    assert psql[:7] == ("exec", "-i", "-n", "target-dev", "target-db-1", "-c", "postgres")
    assert "psql" in psql and "PGPASSWORD" not in " ".join(psql) and "-U" not in psql  # peer login, no secret
    assert {c for c, _ in kube.calls} == {"target-cluster"} and kube.reads == 3
    assert len(list(directory.glob("evidence-*-AUD-01.1.json"))) == 1  # only the final search's evidence


@pytest.mark.parametrize(("kube", "reason"), [
    (Kube(answer=b"chat_owner|false\n"), "TOOL_ERROR"),  # not an admin login: no receipt, no search
])
def test_a_canary_that_did_not_happen_as_registered_is_unknown(tmp_path, kube, reason):
    document, _ = tool.run(registration({"AUD-01.1": AUD}), tmp_path / "run",
                           build=lambda r, e, ev: tool.build_probes(r, e, ev, kube=kube))
    row = {r["test_id"]: r for r in document["results"]}["AUD-01.1"]
    assert row["verdict"] == "UNKNOWN" and row["unknown_reason"] == reason
    assert not [a for _, a in kube.calls if "/log?" in " ".join(a)]


def test_a_login_never_logged_is_absent_only_after_the_window(tmp_path):
    from bravo_controls.runner import RunContext
    from bravo_controls.runner import TestSpec as Spec

    now = {"t": datetime(2026, 11, 7, 12, 0, tzinfo=UTC)}
    kept = []
    kube = Kube(logged_after=10**6)
    probe = tool.DbLoginAuditRun(AUD, kept.append, kube=kube, clock=lambda: now["t"],
                                 sleep=lambda s: now.update(t=now["t"] + timedelta(seconds=s)))
    context = RunContext("run-1", "demo-chat", Environment("env-target", "target", "ref:target"))
    observation = probe(context, Spec("AUD-01.1", "AUD-01", "1", "present", "not_applicable"))
    assert observation.observed == "absent" and len(kept) == 1
    assert 55 <= kube.reads <= 62  # every 3 s for 3 minutes, then stops


@pytest.mark.parametrize("role", ["source", "target"])
def test_the_audit_adapter_belongs_to_both_roles_and_needs_its_fields(tmp_path, role):
    env = Environment(f"env-{role}", role, f"ref:{role}")
    probes = tool.build_probes(registration({"AUD-01.1": AUD}, role=role), env, tool.Evidence(tmp_path), kube=Kube())
    assert isinstance(probes["AUD-01.1"], tool.DbLoginAuditRun)
    with pytest.raises(ValueError, match="registration_fields:AUD-01.1"):
        tool.build_probes(registration({"AUD-01.1": AUD | {"cluster": "Target_DB"}}, role=role), env,
                          tool.Evidence(tmp_path), kube=Kube())


def test_the_example_registration_is_a_valid_target_registration(tmp_path, monkeypatch):
    example = json.loads((ROOT / "operations/controls/registration.example.json").read_text())
    bao_entry(tmp_path)  # the CA and private token files the example points at, here
    entry = example["tests"]["SEC-01.1"]
    entry["ca_file"] = str(tmp_path / "ca.pem")
    for side in ("app", "other"):
        entry[side]["jwt_file"] = str(tmp_path / f"{side}.jwt")
    env = example["environment"]
    iam = example["tests"]["IAM-01.1"]
    probes = tool.build_probes(example, Environment(env["id"], env["role"], env["ref"]), tool.Evidence(tmp_path),
                               environ={iam["access_key_env"]: "a", iam["secret_key_env"]: "b"})
    assert set(probes) == {"IAM-01.1", "SEC-01.1", "AUD-01.1"} and probes["IAM-01.1"].target.endpoint == iam["endpoint"]
    assert set(example) == {"app", "environment", "profile", "tests"}


def test_the_registration_has_exactly_the_documented_fields(tmp_path):
    with pytest.raises(ValueError, match="registration_fields"):
        tool.run(registration({}) | {"credentials": {}}, tmp_path / "run", build=lambda *_: {})
