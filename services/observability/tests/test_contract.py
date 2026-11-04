"""docs/observability의 계약 시험을 기준 구현으로 돌린다. 계약 파일이 바뀌면 CI가 이 시험을 다시 돈다."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from bravo_observability import judge

DOCS = Path(__file__).resolve().parents[3] / "docs/observability"
SCHEMA = json.loads((DOCS / "sli.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
CASES = json.loads((DOCS / "contract-cases.json").read_text())
PROFILES = json.loads((DOCS / "threshold-profiles.json").read_text())


def load(rel):
    return json.loads((DOCS / rel).read_text())


def valid(doc):
    return not list(VALIDATOR.iter_errors(doc))


def patch(doc, ops):
    doc = copy.deepcopy(doc)
    for op in ops:
        *parents, key = op["path"].lstrip("/").split("/")
        target = doc
        for k in parents:
            target = target[k]
        if op["op"] == "remove":
            del target[key]
        elif op["op"] in ("replace", "add"):
            target[key] = op["value"]
        else:
            raise ValueError(f"unsupported op {op['op']}")
    return doc


BASE = CASES["judgement"]["base"]
PROFILE = next(
    p
    for p in PROFILES["profiles"]
    if (p["id"], p["version"]) == (BASE["profile"]["id"], BASE["profile"]["version"])
)


def run_case(case):
    window = None if case.get("window", "") is None else patch(load(BASE["window"]), case.get("patch", []))
    return window, judge(PROFILE, window, **(BASE["inputs"] | case.get("inputs", {})))


def test_schema_is_valid_and_profiles_examples_conform():
    Draft202012Validator.check_schema(SCHEMA)
    assert valid(PROFILES)
    for f in sorted((DOCS / "examples").glob("*.json")):
        assert valid(json.loads(f.read_text())), f.name


def test_case_ids_are_unique():
    ids = [c["id"] for c in CASES["judgement"]["cases"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", CASES["judgement"]["cases"], ids=lambda c: c["id"])
def test_judgement_case(case):
    window, got = run_case(case)
    if window is not None:
        assert valid(window)
    assert valid(got), list(VALIDATOR.iter_errors(got))
    expect = case["expect"]
    assert expect["reasons"] == sorted(expect["reasons"])
    assert (got["verdict"], got["reasons"], got["action"]) == (
        expect["verdict"],
        expect["reasons"],
        expect["action"],
    )


def test_judgement_is_deterministic():
    for case in CASES["judgement"]["cases"]:
        assert run_case(case)[1] == run_case(case)[1]


@pytest.mark.parametrize("case", CASES["schema_reject"]["cases"], ids=lambda c: c["id"])
def test_schema_rejects_window(case):
    assert not valid(patch(load(CASES["schema_reject"]["base_window"]), case["patch"]))


@pytest.mark.parametrize("case", CASES["schema_reject"]["judgement_cases"], ids=lambda c: c["id"])
def test_schema_rejects_judgement(case):
    good = run_case({"id": "base"})[1]
    assert valid(good)
    assert not valid(good | case["judgement"])
