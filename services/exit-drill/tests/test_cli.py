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
        "--induced-restarts", _write(tmp_path, "i.json", {"uid-a1": {"container": "app", "replaced_container_id": "containerd://old"}}),
    ]) == 0
    observation = json.loads(capsys.readouterr().out)
    assert observation["phase"] == "after"
    assert observation["cutover"] == {"writer": "target"}
    assert observation["induced_restarts"] == {"uid-a1": {"container": "app", "replaced_container_id": "containerd://old"}}


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


@pytest.mark.parametrize("command", [["manifests"], ["targets", "--vantage", "app"]])
def test_hand_edited_plan_cannot_be_used(plan_path, capsys, command):
    """차단 항목을 지운 계획이 적용 목록으로 나가면 안 된다."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["manifests"] = plan["manifests"][:1]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert main([command[0], "--plan", str(plan_path), *command[1:]]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "plan" in captured.err


def test_garbage_plan_contents_are_reported_not_a_traceback(tmp_path, capsys):
    broken = _write(tmp_path, "plan.json", {"manifests": [1]})
    assert main(["manifests", "--plan", broken]) == 2
    assert main(["check", "--plan", broken, "--before", broken, "--after", broken]) == 3
    capsys.readouterr()


def _assemble_args(plan_path, tmp_path, probes):
    empty = {"kind": "List", "items": []}
    return [
        "assemble", "--plan", str(plan_path), "--phase", "before",
        "--pods", _write(tmp_path, "pods.json", empty),
        "--policies-start", _write(tmp_path, "s.json", empty),
        "--policies-end", _write(tmp_path, "e.json", empty),
        "--probes", _write(tmp_path, "p.json", probes),
    ]


@pytest.mark.parametrize("probes", [{"app": {"results": [{"route_id": "x"}]}}, {"app": {"pod_uid": "u"}}, {"app": 1}, []])
def test_malformed_probe_file_exits_2(plan_path, tmp_path, capsys, probes):
    assert main(_assemble_args(plan_path, tmp_path, probes)) == 2
    capsys.readouterr()


@pytest.mark.parametrize("snapshot", [{"items": [1]}, {"items": [{"kind": "Pod", "metadata": {"namespace": "target-dev"}}]}, []])
def test_malformed_snapshot_exits_2(inputs, capsys, snapshot):
    inputs["snapshot"].write_text(json.dumps(snapshot), encoding="utf-8")
    assert main(render_args(inputs)) == 2
    capsys.readouterr()


def test_malformed_report_exits_2(plan_path, inputs, tmp_path, capsys):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    report = _write(tmp_path, "report.json", {"plan_digest": plan["digest"]})
    assert main([
        "map", "--inventory", str(inputs["inventory"]), "--candidate", "onprem-demo",
        "--plan", str(plan_path), "--report", report,
    ]) == 2
    capsys.readouterr()


@pytest.mark.parametrize("request_text", ["not json", '{"targets": [{"kind": "shell"}]}', '{"targets": 1}'])
def test_probe_with_bad_request_exits_2_not_1(capsys, request_text):
    """1은 FAIL이다. 처리 못 한 입력이 FAIL로 읽히면 안 된다."""
    assert main(["probe", request_text]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


def test_assemble_with_list_inputs_exits_2(plan_path, tmp_path, capsys):
    args = _assemble_args(plan_path, tmp_path, {})
    args[args.index("--pods") + 1] = _write(tmp_path, "bad.json", ["not pods"])
    assert main(args) == 2
    capsys.readouterr()


def test_spec_with_unhashable_value_exits_2(inputs, tmp_path, capsys):
    doc = json.loads(PROFILE.read_text(encoding="utf-8"))
    doc["routes"][0]["class"] = ["local"]
    args = render_args(inputs)
    args[args.index("--spec") + 1] = _write(tmp_path, "spec.json", doc)
    assert main(args) == 2
    capsys.readouterr()
