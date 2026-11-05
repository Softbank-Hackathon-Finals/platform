"""병합된 T16 코드의 실제 출력으로 계획과 잔존 지도를 만든다. 고정 입력이 낡아도 여기서 드러난다.

T16 소스는 같은 저장소에 있다. 변이 시험은 이 서비스만 복사하므로 그때는 건너뛴다.
"""

import json
import sys
from pathlib import Path

import pytest
from helpers import snapshot_doc

from bravo_exit_drill.aws_endpoints import is_known_service
from bravo_exit_drill.cli import main
from bravo_exit_drill.inventory_input import parse_inventory

T16 = Path(__file__).resolve().parents[2] / "inventory"
FIXTURES = T16 / "tests/fixtures"
PROFILES = T16 / "profiles"
SPEC = Path(__file__).resolve().parents[1] / "profiles/target-dev.json"

pytestmark = pytest.mark.skipif(not T16.exists(), reason="T16 source is not in this copy")


@pytest.fixture(scope="module")
def t16():
    sys.path.insert(0, str(T16 / "src"))
    from bravo_inventory import catalog, cli

    yield catalog, cli
    sys.path.remove(str(T16 / "src"))


def scan(cli, tmp_path: Path, repo: Path, *, exports: Path = FIXTURES / "exports/aws") -> Path:
    output = tmp_path / "inventory.json"
    assert cli.main([
        "scan", "--app-id", "demo-chat", "--repo", str(repo), "--app-path", "app",
        "--source-env", "aws-seoul", "--namespace", "chat", "--exports", str(exports),
        "--profile", str(PROFILES / "onprem-rke2.json"), "--profile", str(PROFILES / "onprem-minimal.json"),
        "--target-exports", f"onprem-demo={FIXTURES / 'exports/onprem'}", "--output", str(output),
    ]) == 0
    return output


@pytest.fixture
def real_inventory(t16, tmp_path) -> Path:
    return scan(t16[1], tmp_path, FIXTURES / "demo-chat")


def write_json(path: Path, value) -> str:
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def run_map(inventory: Path, tmp_path: Path, capsys, *, candidate="onprem-demo", report=None, output_format="json"):
    snapshot = write_json(tmp_path / "snapshot.json", snapshot_doc())
    assert main(["render", "--spec", str(SPEC), "--inventory", str(inventory), "--candidate", candidate, "--snapshot", snapshot]) == 0
    plan = json.loads(capsys.readouterr().out)
    plan_path = write_json(tmp_path / "plan.json", plan)
    args = ["map", "--inventory", str(inventory), "--candidate", candidate, "--plan", plan_path, "--format", output_format]
    if report is not None:
        args += ["--report", write_json(tmp_path / "report.json", {**report, "plan_digest": plan["digest"]})]
    assert main(args) == 0
    text = capsys.readouterr().out
    return plan, (text if output_format == "markdown" else json.loads(text))


def test_fixture_still_matches_what_t16_writes(real_inventory):
    """고정 입력은 T16 출력에서 값만 가린 것이다. 어긋나면 다시 만든다(tests/fixtures/README.md)."""
    fixture_path = Path(__file__).parent / "fixtures/inventory-t16-shakedown.json"
    real = json.loads(real_inventory.read_text(encoding="utf-8"))
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert sorted(real) == sorted(fixture)
    pick = lambda doc: [(i["id"], i["role"], i["service"], i["found_in"], i["grade"], i["grade_by"], i["agent_needed"], i["target"]) for i in doc["items"]]  # noqa: E731
    assert pick(real) == pick(fixture)
    fits = lambda doc: [(c["env_id"], c["env_kind"], c["verdict"], c["missing_capabilities"], c["residual_dependencies"]) for c in doc["candidates"]]  # noqa: E731
    assert fits(real) == fits(fixture)


def test_real_output_reaches_a_map_without_a_drill(real_inventory, tmp_path, capsys):
    """차단 없이 바로 뽑는 지도. 아무것도 확인됐다고 말하지 않고 항목은 하나도 빠지지 않는다."""
    _, result = run_map(real_inventory, tmp_path, capsys)
    items = json.loads(real_inventory.read_text(encoding="utf-8"))["items"]
    got = {e["id"]: e for e in result["entries"]}
    assert {i["id"] for i in items} <= set(got)
    assert result["drill_verdict"] == "NOT_RUN"
    assert "blocked_app_ok" not in result["summary"]["status"]
    deploy_only = [i for i in items if i["found_in"] == "deploy"]
    assert {i["role"] for i in deploy_only} == {"kms", "object_store"}
    assert all(got[i["id"]]["status"] == "unknown" for i in deploy_only)
    secrets = [i for i in items if i["role"] == "secrets" and i["found_in"] == "both"]
    assert len(secrets) == 1
    assert got[secrets[0]["id"]]["status"] == "unverified"
    assert got["residual:cloudflare (edge · tunnel)"]["status"] == "remains"
    assert got["residual:ghcr.io (image pull)"]["status"] == "remains"
    assert got["residual:tailscale (복제 · 제어 경로)"]["source"] == "drill-spec"


def test_real_output_keeps_private_values_out_of_the_map_and_plan(real_inventory, tmp_path, capsys):
    """인벤토리에는 ARN과 계정 번호가 있다. 지도와 계획에는 없어야 공개 절차가 가볍다."""
    text = real_inventory.read_text(encoding="utf-8")
    assert "arn:aws" in text and "000000000000" in text
    plan, result = run_map(real_inventory, tmp_path, capsys)
    for output in (json.dumps(plan), json.dumps(result)):
        assert "arn:aws" not in output
        assert "000000000000" not in output


def test_candidate_gaps_from_t16_appear_on_the_map(real_inventory, tmp_path, capsys):
    """목적지에 없는 기능은 대체 구현 이름이 있어도 남은 문제다. 지도에서 보여야 한다."""
    _, minimal = run_map(real_inventory, tmp_path, capsys, candidate="onprem-minimal")
    assert minimal["candidate_verdict"] == "blocked"
    assert any("s3_compatible" in gap for gap in minimal["candidate_missing_capabilities"])
    _, text = run_map(real_inventory, tmp_path, capsys, candidate="onprem-minimal", output_format="markdown")
    assert "s3_compatible" in text
    _, demo = run_map(real_inventory, tmp_path, capsys)
    assert demo["candidate_missing_capabilities"] == []


def test_real_scan_of_unmapped_sdks_stays_on_the_map(t16, tmp_path, capsys):
    """규칙이 못 정한 SDK와 대체 없는 SDK를 실제로 스캔한다. 추측으로 지우지 않는다."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app/main.py").write_text(
        "import boto3\nboto3.client('sqs')\nboto3.client('textract')\nboto3.client('secretsmanager')\n", encoding="utf-8",
    )
    inventory = scan(t16[1], tmp_path, repo)
    capsys.readouterr()  # T16 CLI가 찍은 요약을 비운다.
    _, result = run_map(inventory, tmp_path, capsys)
    by_role = {e["role"]: e for e in result["entries"] if e["category"] == "source_dependency"}
    assert by_role["messaging"]["status"] == "remains"
    assert by_role["unknown"]["status"] == "unknown"
    assert "textract" in by_role["unknown"]["name"]


def test_plan_measures_every_sdk_t16_maps_to_a_role(t16):
    """T16이 역할을 아는 SDK는 모두 차단을 잴 주소가 있어야 한다. 없으면 조용히 재지 않는다."""
    assert [name for name in t16[0].SERVICE_ROLE if not is_known_service(name)] == []


def test_real_document_parses_as_the_contract_says(real_inventory):
    view = parse_inventory(json.loads(real_inventory.read_text(encoding="utf-8")), candidate="onprem-demo")
    assert view.candidate_verdict == "needs_work"
    assert view.residuals == ("cloudflare (edge · tunnel)", "ghcr.io (image pull)")
    assert {d.role: d.aws_services for d in view.dependencies}["secrets"] == ("secretsmanager",)
