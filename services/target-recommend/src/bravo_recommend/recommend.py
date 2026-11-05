"""Rank the T16 candidates (design 8.11 이전 대상 추천).

Order of authority: (1) the T16 rule verdict — ``blocked`` candidates are *excluded* before any
model sees the list; (2) a deterministic rule ranking of the rest; (3) optionally a model's
ranking of exactly that remaining list, accepted only if it is a permutation of the same ids
with bounded, secret-free reasons. A model can reorder; it can never add, drop or unblock.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Protocol

import jsonschema

SECRET_LIKE = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}|sk-(?:ant-)?[A-Za-z0-9_-]{16,}|://[^/\s@]+:[^/\s@]*@|-----BEGIN ")
VERDICT_ORDER = {"fits": 0, "needs_work": 1}


class AIRankingRejected(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Ranker(Protocol):
    def rank(self, candidates: list[dict]) -> dict:
        """Return {"ranking": [{"env_id", "rank", "reason"}], "model": str}. Only the given ids."""
        ...


def score(candidate: dict) -> list[int]:
    """Smaller is better: verdict, code changes, unknowns, adapters+residual dependencies."""
    counts = candidate.get("grade_counts", {})
    return [VERDICT_ORDER.get(candidate["verdict"], 9), int(counts.get("code_change", 0)), int(counts.get("unknown", 0)),
            int(counts.get("adapter", 0)) + len(candidate.get("residual_dependencies", []))]


def rule_rank(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into (allowed ranked by rule, excluded). Deterministic: ties break on env_id."""
    excluded, allowed = [], []
    for c in candidates:
        if c["verdict"] == "blocked":
            missing = list(c.get("missing_capabilities", []))
            none = int(c.get("grade_counts", {}).get("none", 0))
            reason = (f"missing capabilities: {', '.join(missing)}" if missing else "") + (f"; {none} dependency(ies) without replacement" if none else "")
            excluded.append({"env_id": c["env_id"], "verdict": "blocked", "reason": reason.strip("; ") or "blocked by rule", "missing_capabilities": missing})
        elif c["verdict"] in VERDICT_ORDER:
            allowed.append(c)
        else:
            raise ValueError("unknown verdict from inventory")
    allowed.sort(key=lambda c: (score(c), c["env_id"]))
    ranked = []
    for i, c in enumerate(allowed, 1):
        counts = c.get("grade_counts", {})
        reasons = [f"verdict {c['verdict']}", f"{counts.get('code_change', 0)} code change(s)", f"{counts.get('adapter', 0)} adapter(s)",
                   f"{len(c.get('residual_dependencies', []))} residual dependency(ies)"]
        ranked.append({"env_id": c["env_id"], "env_kind": c["env_kind"], "verdict": c["verdict"], "rule_rank": i, "ai_rank": None,
                       "score": score(c), "grade_counts": dict(counts), "missing_capabilities": list(c.get("missing_capabilities", [])),
                       "residual_dependencies": list(c.get("residual_dependencies", [])), "rule_reasons": reasons, "ai_reason": None})
    return ranked, excluded


def validate_ai_ranking(result: dict, allowed_ids: list[str], excluded_ids: list[str]) -> dict[str, tuple[int, str]]:
    """Accept a model ranking only if it is exactly a permutation of the allowed ids."""
    if not isinstance(result, dict) or not isinstance(result.get("ranking"), list):
        raise AIRankingRejected("AI_SHAPE")
    seen: dict[str, tuple[int, str]] = {}
    for row in result["ranking"]:
        if not isinstance(row, dict) or set(row) - {"env_id", "rank", "reason"} or not {"env_id", "rank"} <= set(row):
            raise AIRankingRejected("AI_ROW_SHAPE")
        env, rank, reason = row["env_id"], row["rank"], str(row.get("reason", ""))
        if env in excluded_ids:
            raise AIRankingRejected("AI_UNBLOCKED_CANDIDATE")  # the rule said no; the model cannot overturn it
        if env not in allowed_ids or env in seen:
            raise AIRankingRejected("AI_UNKNOWN_OR_DUPLICATE_CANDIDATE")
        if type(rank) is not int or rank < 1 or rank > len(allowed_ids):
            raise AIRankingRejected("AI_RANK_RANGE")
        if len(reason) > 512 or "\x00" in reason or SECRET_LIKE.search(reason):
            raise AIRankingRejected("AI_REASON_POLICY")
        seen[env] = (rank, reason)
    if set(seen) != set(allowed_ids) or sorted(r for r, _ in seen.values()) != list(range(1, len(allowed_ids) + 1)):
        raise AIRankingRejected("AI_NOT_A_PERMUTATION")
    return seen


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads(resources.files("bravo_recommend.schemas").joinpath("recommendation.schema.json").read_text("utf-8"))
    return jsonschema.Draft202012Validator(schema)


def recommend(inventory: dict, ranker: Ranker | None = None) -> dict:
    """Build the recommendation document from a T16 inventory (contract v1)."""
    if inventory.get("schema_version") != "1.0" or "candidates" not in inventory:
        raise ValueError("T16 inventory v1 required")
    ranked, excluded = rule_rank(inventory["candidates"])
    ai = {"status": "not_requested", "reason": "RULE_ONLY"}
    ranked_by = "rule"
    if ranker is not None and ranked:
        model_view = [{k: c[k] for k in ("env_id", "env_kind", "verdict", "grade_counts", "missing_capabilities", "residual_dependencies")} for c in ranked]
        try:
            result = ranker.rank(model_view)
            accepted = validate_ai_ranking(result, [c["env_id"] for c in ranked], [e["env_id"] for e in excluded])
        except AIRankingRejected as exc:
            ai = {"status": "rejected", "reason": exc.code}
        except Exception:  # noqa: BLE001 - a failing model is not a failing recommendation
            ai = {"status": "rejected", "reason": "AI_UNAVAILABLE"}
        else:
            for c in ranked:
                c["ai_rank"], c["ai_reason"] = accepted[c["env_id"]]
            ranked.sort(key=lambda c: (c["ai_rank"], c["rule_rank"]))
            ai = {"status": "accepted", "reason": "AI_PERMUTATION_OF_RULE_SET", "model": str(result.get("model", "unknown"))[:128]}
            ranked_by = "rule+ai"
    doc = {"schema_version": "1.0", "app_id": inventory["app"]["id"], "inventory_digest": inventory["digest"], "ranked_by": ranked_by,
           "recommendation": ranked[0]["env_id"] if ranked else None, "candidates": ranked, "excluded": excluded, "ai": ai}
    _validator().validate(doc)
    return doc


class FakeRanker:
    """Test double standing in for the T11 ``plan.propose``-style ranking tool."""

    def __init__(self, result: dict | Exception):
        self.result = result

    def rank(self, candidates):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
