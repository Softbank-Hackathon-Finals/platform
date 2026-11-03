"""창 요약 생성 규칙 (README 4절)."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from bravo_observability import Record, build_window, judge, receive

DOCS = Path(__file__).resolve().parents[3] / "docs/observability"
VALIDATOR = Draft202012Validator(
    json.loads((DOCS / "sli.schema.json").read_text()), format_checker=FormatChecker()
)
PROFILE = json.loads((DOCS / "threshold-profiles.json").read_text())["profiles"][0]
POOLS = {"aws": "source", "onprem": "target"}
START, END = "2026-11-08T05:31:10Z", "2026-11-08T05:31:20Z"


def window(records):
    return build_window(records, plan_id="plan-demo", start=START, end=END, pools=POOLS)


def test_classifies_by_env_header_and_status():
    w = window(
        [
            Record("aws", 201, 100),
            Record("aws", 503, 900),
            Record("onprem", 200, 250),
            Record("onprem", 409, 120),
            Record("onprem", 404, 80),
            Record(None, 502, 30),
            Record("staging", 200, 40),
            Record(None, None, None, "timeout"),
            Record("aws", None, None, "connect"),
        ]
    )
    assert w["pools"]["source"] == {"requests": 2, "http_5xx": 1, "integrity_conflict": 0, "p95_ms": 900}
    assert w["pools"]["target"] == {"requests": 3, "http_5xx": 0, "integrity_conflict": 1, "p95_ms": 250}
    assert w["unattributed"] == {"timeout": 1, "connect": 1, "other": 2}
    assert not list(VALIDATOR.iter_errors(w))


def test_p95_is_nearest_rank():
    w = window([Record("onprem", 200, float(ms)) for ms in range(1, 101)])
    assert w["pools"]["target"]["p95_ms"] == 95.0
    w = window([Record("onprem", 200, float(ms)) for ms in range(1, 21)])
    assert w["pools"]["target"]["p95_ms"] == 19.0


def test_empty_pool_has_null_p95_and_conforms():
    w = window([Record("aws", 200, 10)])
    assert w["pools"]["target"] == {"requests": 0, "http_5xx": 0, "integrity_conflict": 0, "p95_ms": None}
    assert not list(VALIDATOR.iter_errors(w))


def test_receive_overwrites_producer_time():
    w = window([])
    assert receive(w, "2026-11-08T05:31:21Z")["received_at"] == "2026-11-08T05:31:21Z"
    assert w["received_at"] == END


def test_built_window_judges_like_contract_base():
    records = [Record("aws", 201, 120) for _ in range(1800)] + [
        Record("onprem", 201, 260) for _ in range(200)
    ]
    w = receive(window(records), "2026-11-08T05:31:21Z")
    got = judge(
        PROFILE,
        w,
        plan_id="plan-demo",
        state="SHIFT",
        weight_target=10,
        observe_from="2026-11-08T05:31:05Z",
        decided_at="2026-11-08T05:31:22Z",
        prior_unknown=0,
    )
    assert (got["verdict"], got["action"]) == ("PASS", "ADVANCE_ALLOWED")
    assert w == json.loads((DOCS / "examples/window-shift-10.json").read_text())
