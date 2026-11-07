"""operations/controls/run.py: one environment's registered collectors → one ControlRun file."""

import importlib.util
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bravo_controls import Observation
from bravo_controls.aws_qualification import QualifiedAwsProbe
from bravo_controls.object_endpoint import EndpointAnonymousReadProbe
from bravo_controls.openbao import OpenBaoSecretAccessProbe

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
    assert printed["record"].startswith("operations/gate/run.py record --side source --run ")


@pytest.mark.parametrize(("tests", "code"), [
    ({"AUD-01.1": {"adapter": "cloudwatch"}}, "unknown_adapter:AUD-01.1"),   # not wired: refused, never faked
    ({"IAM-01.1": {"adapter": "aws-secret"}}, "unknown_adapter:IAM-01.1"),
    ({"IAM-01.1": "s3-endpoint"}, "unknown_adapter:IAM-01.1"),
])
def test_an_unknown_adapter_is_refused(tmp_path, tests, code):
    with pytest.raises(ValueError, match=code):
        tool.build_probes(registration(tests), None, tool.Evidence(tmp_path))


def test_the_target_object_adapter_takes_its_credentials_from_named_variables_only(tmp_path, capsys):
    from bravo_controls import Environment

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
    from bravo_controls import Environment

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


def test_the_openbao_adapter_logs_in_each_service_account_and_binds_the_accessor(tmp_path):
    from bravo_controls import Environment

    env = Environment("env-target", "target", "ref:target")
    (tmp_path / "ca.pem").write_text(CA)
    for name in ("app", "other"):
        (tmp_path / f"{name}.jwt").write_text(f"jwt-{name}\n")
    logins = []

    def login(origin, ca_pem, mount, role, jwt):
        logins.append((origin, mount, role, jwt))
        return f"token-{role}", f"accessor-{role}"

    who = lambda name: {"namespace": "target-dev", "service_account": name, "uid": f"uid-{name}",  # noqa: E731
                        "role": f"{name}-role", "jwt_file": str(tmp_path / f"{name}.jwt")}
    entry = {"adapter": "openbao", "origin": "https://localhost:8200", "ca_file": str(tmp_path / "ca.pem"),
             "auth_mount": "kubernetes", "mount": "secret", "path": "demo-chat/db", "version": 3,
             "app": who("app"), "other": who("other")}
    probes = tool.build_probes(registration({"SEC-01.1": entry}), env, tool.Evidence(tmp_path), bao_login=login)
    assert isinstance(probes["SEC-01.1"], OpenBaoSecretAccessProbe)
    assert logins == [("https://localhost:8200", "kubernetes", "app-role", "jwt-app"),
                      ("https://localhost:8200", "kubernetes", "other-role", "jwt-other")]
    assert probes["SEC-01.1"].app_client.principal.accessor == "accessor-app-role"


def test_the_registration_has_exactly_the_documented_fields(tmp_path):
    with pytest.raises(ValueError, match="registration_fields"):
        tool.run(registration({}) | {"credentials": {}}, tmp_path / "run", build=lambda *_: {})
