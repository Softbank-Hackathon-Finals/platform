"""aws→onprem 인벤토리가 develop과 같다(ADR-0034 A 작업의 불변 조건).

``fixtures/parity/aws-onprem-develop.json``은 develop의 ``bravo_inventory``로 아래 시나리오를 돌린
문서에서 ``generated_at`` · ``digest``와 계약 v1의 선택 필드(ADR-0034가 더한 것, ``OPTIONAL``)만
뺀 사본이다. 등급 · rationale · agent_needed · target · verdict · missing_capabilities ·
capabilities 값과 출처 · summary · inputs · deploy가 하나라도 달라지면 실패한다. 빼는 선택 필드는
스키마에 선택 필드로 등록돼 있어야 한다(등록 없이 빼면 실패).

다시 만들기(develop 체크아웃의 inventory 환경으로 이 파일을 실행한다):
    uv run --project <develop>/services/inventory python services/inventory/tests/test_develop_parity.py --write <develop SHA>
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from bravo_inventory import build_inventory
from bravo_inventory.deploy_trace import FileAdapter

FIXTURES = Path(__file__).parent / "fixtures"
PROFILES = Path(__file__).resolve().parents[1] / "profiles"
GOLDEN = FIXTURES / "parity" / "aws-onprem-develop.json"
SCHEMA = Path(__file__).resolve().parents[1] / "src" / "bravo_inventory" / "schemas" / "inventory.schema.json"
NOW = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)

# Optional v1 fields added by ADR-0034 (A3 source adapters, A4 catalog hub), per location.
OPTIONAL = {
    "code_ref": {"source_kind", "cloud_dependency"},
    "deploy_ref": {"cloud_dependency"},
    "target": {"source_kind", "basis"},
    "candidate": {"source_kind", "migration"},
    "capability": {"basis"},
}


def scenarios() -> dict[str, dict]:
    aws = FIXTURES / "exports" / "aws"
    common = {
        "app_id": "demo-chat", "repo": FIXTURES / "demo-chat", "repo_ref": "Softbank-Hackathon-Finals/demo-chat", "app_path": "app",
        "source_env": {"env_id": "aws-seoul", "env_kind": "aws", "namespace": "chat"}, "source_sha": "1" * 40, "now": NOW,
    }
    rke2, minimal = PROFILES / "onprem-rke2.json", PROFILES / "onprem-minimal.json"
    return {
        # Two onprem candidates, CRD/workload observation for one of them.
        "observed_exports": build_inventory(**common, adapter=FileAdapter(aws), profiles=[rke2, minimal],
                                            exports={"onprem-demo": FIXTURES / "exports" / "onprem"}),
        # Raw API answers: postgres operator absent (overrides the declared value), s3/secret/cis unknown (keep it).
        "api_answers": build_inventory(**common, adapter=FileAdapter(aws), profiles=[rke2],
                                       exports={"onprem-demo": FIXTURES / "parity" / "onprem-api"}),
        # Code only, no candidate: the primary item list falls back to onprem.
        "code_only_no_profile": build_inventory(**common, adapter=None, profiles=[]),
    }


def _strip(obj: dict, keys: set[str], where: str, seen: dict[str, set[str]]) -> dict:
    seen.setdefault(where, set()).update(k for k in obj if k in keys)
    return {k: v for k, v in obj.items() if k not in keys}


def project(doc: dict, seen: dict[str, set[str]] | None = None) -> dict:
    """The document without time, digest and the ADR-0034 optional fields."""
    seen = {} if seen is None else seen
    out = {k: v for k, v in doc.items() if k not in {"generated_at", "digest"}}
    out["items"] = [{
        **it,
        "code_refs": [_strip(r, OPTIONAL["code_ref"], "code_ref", seen) for r in it["code_refs"]],
        "deploy_refs": [_strip(r, OPTIONAL["deploy_ref"], "deploy_ref", seen) for r in it["deploy_refs"]],
        "target": _strip(it["target"], OPTIONAL["target"], "target", seen),
    } for it in doc["items"]]
    out["candidates"] = [{
        **_strip(c, OPTIONAL["candidate"], "candidate", seen),
        "capabilities": {k: _strip(v, OPTIONAL["capability"], "capability", seen) for k, v in c["capabilities"].items()},
    } for c in doc["candidates"]]
    return out


def _declared(schema: dict) -> dict[str, set[str]]:
    d, item = schema["$defs"], schema["$defs"]["Item"]["properties"]
    return {
        "code_ref": set(item["code_refs"]["items"].get("properties", {})),
        "deploy_ref": set(item["deploy_refs"]["items"].get("properties", {})),
        "target": set(item["target"].get("properties", {})),
        "candidate": set(d["Fit"].get("properties", {})),
        "capability": set(d["Capability"].get("properties", {})),
    }


def test_aws_to_onprem_matches_develop():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["scenarios"]
    seen: dict[str, set[str]] = {}
    actual = {name: project(doc, seen) for name, doc in scenarios().items()}
    assert set(actual) == set(golden)
    for name in golden:
        assert actual[name] == golden[name], name


def test_dropped_fields_are_registered_optional_fields():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    seen: dict[str, set[str]] = {}
    for doc in scenarios().values():
        project(doc, seen)
    declared = _declared(schema)
    for where, keys in seen.items():
        assert keys <= declared[where], (where, keys - declared[where])
    # Optional means not required (contract v1: no new required field within "1.0").
    assert not set(schema["$defs"]["Fit"]["required"]) & OPTIONAL["candidate"]
    assert not set(schema["$defs"]["Capability"]["required"]) & OPTIONAL["capability"]


if __name__ == "__main__":  # regenerate from a develop checkout (see the module docstring)
    if sys.argv[1:2] != ["--write"] or len(sys.argv) != 3:
        raise SystemExit("usage: test_develop_parity.py --write <develop SHA>")
    body = {"develop": sys.argv[2], "scenarios": {name: project(doc) for name, doc in scenarios().items()}}
    GOLDEN.write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
