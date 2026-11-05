import json
from pathlib import Path

import jsonschema
import pytest

from bravo_recommend.cli import main
from bravo_recommend.recommend import (
    AIRankingRejected,
    FakeRanker,
    recommend,
    rule_rank,
    validate_ai_ranking,
)

FIXTURE = Path(__file__).parent / "fixtures/t16-inventory-demo-chat.json"


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
    from bravo_recommend.recommend import _validator
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
