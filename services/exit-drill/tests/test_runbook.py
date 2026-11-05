"""실행 안내의 명령이 실제 도구와 맞는지 잰다. 안내대로 쳤는데 없는 옵션이면 실제 실행 중에 멈춘다."""

import json
import re
import shlex
import sys
from pathlib import Path

import pytest
from helpers import inventory_doc, snapshot_doc, spec_doc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import real_run  # noqa: E402

from bravo_exit_drill.cli import _parser as cli_parser  # noqa: E402
from bravo_exit_drill.cluster import parse_snapshot  # noqa: E402
from bravo_exit_drill.control_pod import NAME, render_control_pod  # noqa: E402
from bravo_exit_drill.inventory_input import parse_inventory  # noqa: E402
from bravo_exit_drill.policy import build_plan  # noqa: E402
from bravo_exit_drill.spec import parse_spec  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
RUNBOOK = REPO / "docs/operations/t27-exit-drill-real-run.md"
IMAGE = "ghcr.io/softbank-hackathon-finals/demo-chat@sha256:" + "a" * 64
ROLLBACK_LABEL = "app.kubernetes.io/part-of=bravo-exit-drill"

pytestmark = pytest.mark.skipif(not RUNBOOK.exists(), reason="the runbook is not in this copy")


def commands() -> list[str]:
    """코드 블록의 명령을 줄 이음까지 합쳐 한 줄씩 돌려준다. 시험 수집 때 불리므로 문서가 없으면 빈 목록이다."""
    if not RUNBOOK.exists():
        return []
    blocks = re.findall(r"```sh\n(.*?)```", RUNBOOK.read_text(encoding="utf-8"), re.S)
    return [line.strip() for block in blocks for line in block.replace("\\\n", " ").splitlines() if line.strip()]


def arguments(command: str, marker: str) -> list[str]:
    """쉘 변수와 리디렉션을 걷어 내고 도구에 넘어갈 인자만 남긴다."""
    tail = command.split(marker, 1)[1]
    tail = re.split(r"\s(?:>|\|\||;|2>)", tail)[0]
    return shlex.split(tail, comments=True)


def package_commands() -> list[str]:
    return [c for c in commands() if c.startswith("uv run bravo-exit-drill ")]


def tool_commands() -> list[str]:
    return [c.removeprefix("APPLIED_AT=$(").removesuffix(")") for c in commands() if re.match(r"(APPLIED_AT=\$\()?drill ", c)]


@pytest.mark.parametrize("command", package_commands(), ids=lambda c: c[:50])
def test_package_commands_parse(command):
    assert cli_parser().parse_args(arguments(command, "bravo-exit-drill "))


def test_runbook_uses_the_package_commands_in_order():
    used = [arguments(c, "bravo-exit-drill ")[0] for c in package_commands()]
    assert used == ["control-pod", "render", "map", "manifests", "check", "map"]


@pytest.mark.parametrize("command", tool_commands(), ids=lambda c: c[:50])
def test_operator_tool_commands_parse(command):
    assert real_run._parser().parse_args(["--plan", "plan.json", *arguments(command, "drill ")])


def test_runbook_steps_through_the_operator_tool():
    used = [arguments(c, "drill ")[0] for c in tool_commands()]
    assert used == ["collect", "clock", "restart", "collect"]


def plan() -> dict:
    return build_plan(
        parse_spec(spec_doc()),
        parse_inventory(inventory_doc(), candidate="onprem-demo"),
        parse_snapshot(snapshot_doc(), namespace="target-dev"),
    )


def test_rollback_label_really_is_on_what_the_drill_creates():
    key, value = ROLLBACK_LABEL.split("=")
    assert any(ROLLBACK_LABEL in c and "delete networkpolicy,pod" in c for c in commands())
    for manifest in plan()["manifests"]:
        assert manifest["metadata"]["labels"][key] == value
    assert render_control_pod(parse_spec(spec_doc()), IMAGE)["metadata"]["labels"][key] == value


def test_runbook_waits_for_the_pod_the_manifest_creates():
    assert any(f"pod/{NAME}" in c for c in commands())


def test_candidate_in_the_runbook_is_the_profile_env_id():
    profile = json.loads((REPO / "services/inventory/profiles/onprem-rke2.json").read_text(encoding="utf-8"))
    assert profile["env_id"] == "onprem-demo"
    assert any("--candidate onprem-demo" in c for c in commands())
    assert any("onprem-demo=$OUTPUT_DIR/exports/target" in c for c in commands())


def test_rollout_commands_name_the_deployments_the_spec_selects():
    restart = next(c for c in commands() if "rollout restart" in c)
    for name in ("deployment/demo-chat", "deployment/target-pgbouncer"):
        assert name in restart


def test_runbook_never_tells_anyone_to_touch_the_database_or_the_namespace():
    for command in commands():
        assert "delete namespace" not in command
        assert "target-db" not in command
        assert not re.search(r"\b(DROP|TRUNCATE)\b", command)
