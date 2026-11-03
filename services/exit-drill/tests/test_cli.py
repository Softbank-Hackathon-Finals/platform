"""명령줄 도구. 결과는 표준 출력으로만 내고, 종료 코드로 판정을 구분한다."""

import json

import pytest
from helpers import PROFILE, inventory_doc, network_policy, snapshot_doc

from bravo_exit_drill.cli import main


@pytest.fixture
def inputs(tmp_path):
    paths = {
        "inventory": tmp_path / "inventory.json",
        "snapshot": tmp_path / "snapshot.json",
    }
    paths["inventory"].write_text(json.dumps(inventory_doc()), encoding="utf-8")
    paths["snapshot"].write_text(json.dumps(snapshot_doc()), encoding="utf-8")
    return paths


def render_args(inputs) -> list[str]:
    return [
        "render", "--spec", str(PROFILE), "--inventory", str(inputs["inventory"]),
        "--candidate", "onprem-demo", "--snapshot", str(inputs["snapshot"]),
    ]


@pytest.fixture
def plan_path(inputs, tmp_path, capsys):
    assert main(render_args(inputs)) == 0
    path = tmp_path / "plan.json"
    path.write_text(capsys.readouterr().out, encoding="utf-8")
    return path


def test_render_prints_plan(inputs, capsys):
    assert main(render_args(inputs)) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema_version"] == "exit-drill-plan/v1"
    assert plan["manifests"]


def test_render_refusal_lists_codes_and_exits_1(inputs, capsys):
    leak = network_policy("old-allow", {"matchLabels": {"app": "demo-chat"}}, egress=[{}])
    inputs["snapshot"].write_text(json.dumps(snapshot_doc(policies=[leak])), encoding="utf-8")
    assert main(render_args(inputs)) == 1
    refused = json.loads(capsys.readouterr().out)["refused"]
    assert refused == [{"code": "UNDECLARED_EGRESS", "subject": "old-allow:demo-chat-1"}]


def test_render_with_missing_file_exits_2_without_traceback(inputs, capsys):
    inputs["inventory"].unlink()
    assert main(render_args(inputs)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert str(inputs["inventory"].parent) not in captured.err


def test_render_with_broken_contract_exits_2(inputs, capsys):
    doc = inventory_doc()
    doc["schema_version"] = "9"
    inputs["inventory"].write_text(json.dumps(doc), encoding="utf-8")
    assert main(render_args(inputs)) == 2
    assert "schema_version" in capsys.readouterr().err


def test_manifests_prints_applicable_list(plan_path, capsys):
    assert main(["manifests", "--plan", str(plan_path)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["kind"] == "List"
    assert {i["kind"] for i in listed["items"]} == {"NetworkPolicy"}


def test_targets_prints_probe_request(plan_path, capsys):
    assert main(["targets", "--plan", str(plan_path), "--vantage", "db"]) == 0
    request = json.loads(capsys.readouterr().out)
    assert [t["route_id"] for t in request["targets"]] == ["replication-forward"]
    assert request["timeout"] == 3


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_assemble_builds_observation(plan_path, tmp_path, capsys):
    empty = {"kind": "List", "items": []}
    assert main([
        "assemble", "--plan", str(plan_path), "--phase", "after",
        "--pods", _write(tmp_path, "pods.json", empty),
        "--policies-start", _write(tmp_path, "s.json", empty),
        "--policies-end", _write(tmp_path, "e.json", empty),
        "--probes", _write(tmp_path, "p.json", {}),
        "--replication", _write(tmp_path, "r.json", []),
        "--cutover", _write(tmp_path, "c.json", {"writer": "target"}),
        "--policy-applied-at", "2026-11-07T03:00:00+00:00",
    ]) == 0
    observation = json.loads(capsys.readouterr().out)
    assert observation["phase"] == "after"
    assert observation["cutover"] == {"writer": "target"}


def test_check_with_no_usable_evidence_is_unknown_exit_3(plan_path, tmp_path, capsys):
    missing = str(tmp_path / "nothing.json")
    assert main(["check", "--plan", str(plan_path), "--before", missing, "--after", missing]) == 3
    assert json.loads(capsys.readouterr().out)["verdict"] == "UNKNOWN"


def test_check_with_broken_plan_exits_2(tmp_path, capsys):
    broken = _write(tmp_path, "plan.json", ["not a plan"])
    assert main(["check", "--plan", broken, "--before", broken, "--after", broken]) == 2


@pytest.mark.parametrize("fmt", ["json", "markdown"])
def test_map_without_report(plan_path, inputs, capsys, fmt):
    assert main([
        "map", "--inventory", str(inputs["inventory"]), "--candidate", "onprem-demo",
        "--plan", str(plan_path), "--format", fmt,
    ]) == 0
    out = capsys.readouterr().out
    assert "NOT_RUN" in out


def test_map_with_foreign_report_exits_2(plan_path, inputs, tmp_path, capsys):
    report = _write(tmp_path, "report.json", {"verdict": "PASS", "plan_digest": "sha256:other", "checks": []})
    assert main([
        "map", "--inventory", str(inputs["inventory"]), "--candidate", "onprem-demo",
        "--plan", str(plan_path), "--report", report,
    ]) == 2
    capsys.readouterr()


def test_probe_command_runs_targets(capsys):
    assert main(["probe", json.dumps({"targets": [{"route_id": "d", "kind": "dns", "host": "localhost"}], "timeout": 1})]) == 0
    assert json.loads(capsys.readouterr().out)[0]["state"] == "resolved"
