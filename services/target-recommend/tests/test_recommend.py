import json
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from bravo_recommend.cli import main
from bravo_recommend.recommend import (
    AIRankingRejected,
    FakeRanker,
    _validator,
    exclusion,
    recommend,
    rule_rank,
    validate_ai_ranking,
)

FIXTURE = Path(__file__).parent / "fixtures/t16-inventory-demo-chat.json"
T16 = Path(__file__).resolve().parents[2] / "inventory"


def _inventory():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    base = doc["candidates"][0]
    minimal = dict(base, env_id="onprem-minimal", verdict="blocked", missing_capabilities=["s3_compatible", "secret_store"],
                   grade_counts=dict(base["grade_counts"], none=2), residual_dependencies=["cloudflare (edge · tunnel)"])
    gcp = dict(base, env_id="gcp-demo", env_kind="gcp", verdict="needs_work", grade_counts=dict(base["grade_counts"], code_change=3),
               residual_dependencies=[])
    fits = dict(base, env_id="onprem-full", verdict="fits", grade_counts=dict(base["grade_counts"], code_change=0, adapter=0), residual_dependencies=[])
    doc["candidates"] = [base, minimal, gcp, fits]
    return doc


def test_blocked_candidates_are_excluded_before_ranking():
    ranked, excluded = rule_rank(_inventory()["candidates"])
    assert [e["env_id"] for e in excluded] == ["onprem-minimal"] and "s3_compatible" in excluded[0]["reason"]
    assert [c["env_id"] for c in ranked] == ["onprem-full", "onprem-demo", "gcp-demo"]
    assert [c["rule_rank"] for c in ranked] == [1, 2, 3] and ranked[0]["score"] < ranked[1]["score"]


def test_rule_ranking_is_deterministic_and_ties_break_on_id():
    inv = _inventory()
    twin = dict(inv["candidates"][0], env_id="aaa-demo")
    inv["candidates"].append(twin)
    ranked, _ = rule_rank(inv["candidates"])
    ids = [c["env_id"] for c in ranked]
    assert ids.index("aaa-demo") < ids.index("onprem-demo") and ids == [c["env_id"] for c in rule_rank(list(reversed(inv["candidates"])))[0]]


def test_rule_only_document_validates_and_recommends_top():
    doc = recommend(_inventory())
    assert doc["recommendation"] == "onprem-full" and doc["ranked_by"] == "rule" and doc["ai"]["status"] == "not_requested"
    assert doc["inventory_digest"].startswith("sha256:") and all(c["ai_rank"] is None for c in doc["candidates"])


def test_ai_may_reorder_but_only_within_the_allowed_set():
    ranker = FakeRanker({"model": "fixture-model", "ranking": [
        {"env_id": "onprem-demo", "rank": 1, "reason": "operations team already runs RKE2"},
        {"env_id": "onprem-full", "rank": 2, "reason": "more components to operate"},
        {"env_id": "gcp-demo", "rank": 3, "reason": "most code changes"}]})
    doc = recommend(_inventory(), ranker)
    assert doc["ranked_by"] == "rule+ai" and doc["recommendation"] == "onprem-demo"
    assert [c["env_id"] for c in doc["candidates"]] == ["onprem-demo", "onprem-full", "gcp-demo"]
    assert doc["candidates"][0]["rule_rank"] == 2 and doc["candidates"][0]["ai_rank"] == 1  # both kept for the console
    assert [e["env_id"] for e in doc["excluded"]] == ["onprem-minimal"]


@pytest.mark.parametrize("ranking, code", [
    ([{"env_id": "onprem-minimal", "rank": 1}, {"env_id": "onprem-demo", "rank": 2}, {"env_id": "onprem-full", "rank": 3}, {"env_id": "gcp-demo", "rank": 4}], "AI_UNBLOCKED_CANDIDATE"),
    ([{"env_id": "onprem-demo", "rank": 1}, {"env_id": "onprem-full", "rank": 2}], "AI_NOT_A_PERMUTATION"),
    ([{"env_id": "onprem-demo", "rank": 1}, {"env_id": "onprem-full", "rank": 1}, {"env_id": "gcp-demo", "rank": 2}], "AI_NOT_A_PERMUTATION"),
    ([{"env_id": "aws-prod", "rank": 1}, {"env_id": "onprem-demo", "rank": 2}, {"env_id": "onprem-full", "rank": 3}], "AI_UNKNOWN_OR_DUPLICATE_CANDIDATE"),
    ([{"env_id": "onprem-demo", "rank": 1, "reason": "postgresql://u:pw@h/db"}, {"env_id": "onprem-full", "rank": 2}, {"env_id": "gcp-demo", "rank": 3}], "AI_REASON_POLICY"),
    ([{"env_id": "onprem-demo", "rank": 1, "deploy": True}, {"env_id": "onprem-full", "rank": 2}, {"env_id": "gcp-demo", "rank": 3}], "AI_ROW_SHAPE"),
])
def test_ai_cannot_unblock_add_drop_or_smuggle(ranking, code):
    with pytest.raises(AIRankingRejected) as info:
        validate_ai_ranking({"ranking": ranking}, ["onprem-full", "onprem-demo", "gcp-demo"], ["onprem-minimal"])
    assert info.value.code == code
    doc = recommend(_inventory(), FakeRanker({"ranking": ranking}))
    assert doc["ai"] == {"status": "rejected", "reason": code} and doc["ranked_by"] == "rule" and doc["recommendation"] == "onprem-full"


def test_failing_model_falls_back_to_rules():
    doc = recommend(_inventory(), FakeRanker(RuntimeError("model down")))
    assert doc["ai"]["reason"] == "AI_UNAVAILABLE" and doc["recommendation"] == "onprem-full"


def test_schema_rejects_a_blocked_candidate_in_the_ranked_list():
    doc = recommend(_inventory())
    doc["candidates"][0]["verdict"] = "blocked"
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(doc)


def test_cli_round_trip(tmp_path, capsys):
    inv = tmp_path / "inventory.json"
    inv.write_text(json.dumps(_inventory()), encoding="utf-8")
    ai = tmp_path / "ai.json"
    ai.write_text(json.dumps({"model": "m", "ranking": [{"env_id": "gcp-demo", "rank": 1}, {"env_id": "onprem-demo", "rank": 2}, {"env_id": "onprem-full", "rank": 3}]}), encoding="utf-8")
    assert main(["--inventory", str(inv), "--output", str(tmp_path / "rec.json"), "--ai-ranking", str(ai)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["recommendation"] == "gcp-demo" and out["excluded"] == ["onprem-minimal"] and out["ranked_by"] == "rule+ai"
    with pytest.raises(SystemExit):
        main(["--inventory", str(inv), "--output", str(tmp_path / "rec.json")])


def _real_t16_inventory(tmp_path: Path) -> dict:
    """Run the real T16 ``bravo-inventory scan`` on its own fixtures (no hand-made candidates).

    ``onprem-bare`` registers none of the capabilities T16 checks (s3_compatible, secret_store,
    postgres_operator), and the app gets one SQS client so one item is graded ``none`` on onprem.
    """
    repo = tmp_path / "demo-chat"
    shutil.copytree(T16 / "tests/fixtures/demo-chat", repo)
    (repo / "app/queue.py").write_text('import boto3\n\nqueue = boto3.client("sqs")\n', encoding="utf-8")
    bare = tmp_path / "onprem-bare.json"
    bare.write_text(json.dumps({"env_id": "onprem-bare", "env_kind": "onprem", "provides": {"k8s": "k3s"},
                                "residual_dependencies": ["cloudflare (edge · tunnel)"]}), encoding="utf-8")
    out = tmp_path / "inventory.json"
    cli = Path(sys.executable).with_name("bravo-inventory")
    subprocess.run([str(cli), "scan", "--app-id", "demo-chat", "--repo", str(repo), "--app-path", "app", "--source-env", "aws-seoul",
                    "--namespace", "chat", "--exports", str(T16 / "tests/fixtures/exports/aws"),
                    "--profile", str(T16 / "profiles/onprem-rke2.json"), "--profile", str(bare),
                    "--target-exports", f"onprem-demo={T16 / 'tests/fixtures/exports/onprem'}", "--output", str(out)],
                   check=True, capture_output=True, text=True)
    return json.loads(out.read_text(encoding="utf-8"))


def test_real_t16_blocked_candidate_with_long_missing_capabilities_still_validates(tmp_path):
    inventory = _real_t16_inventory(tmp_path)
    bare = next(c for c in inventory["candidates"] if c["env_id"] == "onprem-bare")
    assert bare["verdict"] == "blocked" and bare["grade_counts"]["none"] == 1 and len(bare["missing_capabilities"]) == 3
    # The input that used to break the 200-char reason: T16 writes each gap as a sentence.
    assert len("; ".join(bare["missing_capabilities"])) > 150

    doc = recommend(inventory)  # validates against the strict schema

    # The SQS 'none' item blocks every onprem target, so nothing is left to recommend.
    assert doc["recommendation"] is None and doc["candidates"] == []
    ex, demo = sorted(doc["excluded"], key=lambda e: e["env_id"])
    assert demo["env_id"] == "onprem-demo" and demo["reason_codes"] == ["NO_REPLACEMENT"] and demo["missing_capability_keys"] == []
    assert ex["env_id"] == "onprem-bare" and ex["reason_codes"] == ["MISSING_CAPABILITY", "NO_REPLACEMENT"]
    assert ex["missing_capability_keys"] == ["postgres_operator", "s3_compatible", "secret_store"]
    assert ex["no_replacement_count"] == 1 and ex["missing_capabilities"] == bare["missing_capabilities"]
    assert ex["reason"] == "missing capabilities: postgres_operator, s3_compatible, secret_store; 1 dependency(ies) without replacement"


def test_real_t16_inventory_through_the_cli(tmp_path, capsys):
    inv = tmp_path / "t16.json"
    inv.write_text(json.dumps(_real_t16_inventory(tmp_path), ensure_ascii=False), encoding="utf-8")
    assert main(["--inventory", str(inv), "--output", str(tmp_path / "rec.json")]) == 0
    assert json.loads(capsys.readouterr().out)["excluded"] == ["onprem-demo", "onprem-bare"]
    _validator().validate(json.loads((tmp_path / "rec.json").read_text(encoding="utf-8")))


def test_reason_summary_stays_bounded_without_cutting_text():
    keys = [f"capability_{i:02d}_with_a_long_registered_name" for i in range(12)]
    ex = exclusion({"env_id": "onprem-x", "verdict": "blocked", "grade_counts": {"none": 2},
                    "missing_capabilities": [f"role 대체를 뒷받침할 '{k}' 기능이 등록되지 않음" for k in keys] + ["형식을 알 수 없는 문장"]})
    assert ex["reason"] == "13 missing capability(ies), see missing_capability_keys; 2 dependency(ies) without replacement"
    assert ex["missing_capability_keys"] == sorted(keys) and len(ex["missing_capabilities"]) == 13


def test_unrecognised_missing_entry_is_counted_not_guessed():
    ex = exclusion({"env_id": "onprem-x", "verdict": "blocked", "missing_capabilities": ["secret_store", "'a' 또는 'b' 중 하나"]})
    assert ex["missing_capability_keys"] == ["secret_store"] and ex["reason"] == "missing capabilities: secret_store, 1 unrecognised"
    assert exclusion({"env_id": "onprem-x", "verdict": "blocked"})["reason_codes"] == ["BLOCKED_BY_RULE"]


def test_schema_still_rejects_an_overlong_reason():
    doc = recommend(_inventory())
    doc["excluded"][0]["reason"] = "x" * 201
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(doc)
