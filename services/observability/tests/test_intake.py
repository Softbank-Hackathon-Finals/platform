"""창 수신 경로 (계약 3절 "소비자가 확인할 것"). 저장은 참조 구현 SqliteWindowStore로 본다."""

import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bravo_observability import Record, build_window, judge
from bravo_observability.api import Prometheus, bearer_token, create_app
from bravo_observability.contract import ContractViolation, validate
from bravo_observability.intake import (
    MAX_CLOCK_AHEAD_SECONDS,
    IntakeRejected,
    SqliteWindowStore,
    accept,
)
from bravo_observability.intake_api import MAX_WINDOW_BYTES, create_intake_app
from bravo_observability.intake_api import from_env as intake_from_env

DOCS = Path(__file__).resolve().parents[3] / "docs/observability"
PROFILE = json.loads((DOCS / "threshold-profiles.json").read_text())["profiles"][0]
CONSOLE = {"Authorization": f"Bearer {'c' * 40}"}
LOADGEN = {"Authorization": f"Bearer {'w' * 40}"}
POOLS = {"aws": "source", "onprem": "target"}
NOW = "2026-11-08T05:31:21Z"


def window(start="2026-11-08T05:31:10Z", end="2026-11-08T05:31:20Z", plan_id="plan-demo", **extra):
    records = [Record("aws", 200, 100)] * 1800 + [Record("onprem", 200, 120)] * 200
    w = build_window(records, plan_id=plan_id, start=start, end=end, pools=POOLS)
    return {**w, **extra}


def app(store, clock=lambda: NOW):
    return TestClient(
        create_intake_app(
            store,
            authorize=bearer_token("w" * 40),
            is_active_plan=lambda plan_id: plan_id == "plan-demo",
            clock=clock,
        )
    )


def test_accepts_and_overwrites_received_at_with_platform_clock():
    store = SqliteWindowStore()
    r = app(store).post("/v1/observability/windows", json=window(received_at="2099-01-01T00:00:00Z"), headers=LOADGEN)
    assert r.status_code == 201
    assert r.json() == {"plan_id": "plan-demo", "window_start": "2026-11-08T05:31:10Z", "received_at": NOW}
    assert store.latest("plan-demo")["received_at"] == NOW


def test_stored_window_feeds_judge():
    store = SqliteWindowStore()
    app(store).post("/v1/observability/windows", json=window(), headers=LOADGEN)
    j = judge(
        PROFILE,
        store.latest("plan-demo"),
        plan_id="plan-demo",
        state="SHIFT",
        weight_target=10,
        observe_from="2026-11-08T05:31:00Z",
        decided_at="2026-11-08T05:31:22Z",
        prior_unknown=0,
    )
    assert (j["verdict"], j["action"]) == ("PASS", "ADVANCE_ALLOWED"), j


def test_requires_loadgen_token_and_default_denies():
    store = SqliteWindowStore()
    c = app(store)
    assert c.post("/v1/observability/windows", json=window()).status_code == 401
    assert c.post("/v1/observability/windows", json=window(), headers=CONSOLE).status_code == 401
    default = TestClient(create_intake_app(store, is_active_plan=lambda _p: True))
    assert default.post("/v1/observability/windows", json=window(), headers=LOADGEN).status_code == 401
    assert store.latest("plan-demo") is None


def test_intake_and_console_are_separate_apps():
    # 수신 API에는 조회 경로가 없고, 조회 API에는 수신 경로가 없다
    assert app(SqliteWindowStore()).get("/v1/observability/environments", headers=CONSOLE).status_code == 404
    prom = Prometheus(httpx.Client(base_url="https://prom"))
    console = TestClient(create_app(prom, ["aws"], authorize=bearer_token("c" * 40)))
    assert console.post("/v1/observability/windows", json=window(), headers=CONSOLE).status_code in (404, 405)


@pytest.mark.parametrize(
    "env",
    [
        {"BRAVO_WINDOW_TOKEN": "short", "BRAVO_WINDOW_PLANS": "plan-demo"},
        {"BRAVO_WINDOW_TOKEN": "w" * 40, "BRAVO_WINDOW_PLANS": " , "},
        {"BRAVO_WINDOW_TOKEN": "w" * 40, "BRAVO_WINDOW_PLANS": "plan demo"},
        {"BRAVO_WINDOW_TOKEN": "w" * 40},
    ],
)
def test_from_env_refuses_incomplete_settings(monkeypatch, tmp_path, env):
    monkeypatch.setenv("BRAVO_WINDOW_DB", str(tmp_path / "w.db"))
    for k in ("BRAVO_WINDOW_TOKEN", "BRAVO_WINDOW_PLANS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises((KeyError, ValueError)):
        intake_from_env()


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (b"{", 400, "invalid_json"),
        (json.dumps({**window(), "source": "target-app"}).encode(), 422, "invalid_window"),
        (json.dumps({**window(), "extra": 1}).encode(), 422, "invalid_window"),
        (json.dumps(window(start="2026-11-08 05:31:10")).encode(), 422, "invalid_window"),
        (json.dumps(window(start="2026-11-08T05:31:20Z")).encode(), 422, "invalid_window"),
        (json.dumps(window(plan_id="plan-other")).encode(), 404, "plan_not_accepting_windows"),
        (b" " * (MAX_WINDOW_BYTES + 1), 413, "window_too_large"),
    ],
)
def test_rejections(body, status, detail):
    store = SqliteWindowStore()
    r = app(store).post("/v1/observability/windows", content=body, headers=LOADGEN)
    assert (r.status_code, r.json()["detail"]) == (status, detail)
    assert store.latest("plan-demo") is None and store.latest("plan-other") is None


def test_duplicate_and_older_windows_cannot_replace_latest():
    store = SqliteWindowStore()
    c = app(store, clock=lambda: "2026-11-08T05:31:31Z")
    post = lambda w: c.post("/v1/observability/windows", json=w, headers=LOADGEN)  # noqa: E731
    assert post(window(start="2026-11-08T05:31:20Z", end="2026-11-08T05:31:30Z")).status_code == 201
    # 같은 start는 내용이 달라도 한 번만
    failing = window(start="2026-11-08T05:31:20Z", end="2026-11-08T05:31:30Z")
    failing["pools"]["target"]["integrity_conflict"] = 1
    assert post(failing).json()["detail"] == "duplicate"
    # 이미 받은 창보다 이른 정상 창으로 장애를 가리지 못한다
    r = post(window(start="2026-11-08T05:31:10Z", end="2026-11-08T05:31:20Z"))
    assert (r.status_code, r.json()["detail"]) == (409, "out_of_order")
    assert store.latest("plan-demo")["window"]["start"] == "2026-11-08T05:31:20Z"


def test_time_comparison_is_by_instant_not_string():
    store = SqliteWindowStore()
    now = "2026-11-08T05:31:31Z"
    accept(store, window(start="2026-11-08T05:31:20Z", end="2026-11-08T05:31:30Z"), now=now, is_active_plan=lambda _p: True)
    # 같은 순간을 다른 오프셋으로 쓴 창도 같은 창이다
    with pytest.raises(IntakeRejected, match="duplicate"):
        accept(store, window(start="2026-11-08T14:31:20+09:00", end="2026-11-08T14:31:30+09:00"), now=now, is_active_plan=lambda _p: True)
    accept(store, window(start="2026-11-08T05:31:30.5Z", end="2026-11-08T05:31:30.75Z"), now=now, is_active_plan=lambda _p: True)
    assert store.latest("plan-demo")["window"]["end"] == "2026-11-08T05:31:30.75Z"


def test_future_window_is_rejected_and_does_not_block_later_windows():
    store = SqliteWindowStore()
    future = window(start="2099-01-01T00:00:00Z", end="2099-01-01T00:00:10Z")
    with pytest.raises(IntakeRejected, match="clock_ahead"):
        accept(store, future, now=NOW, is_active_plan=lambda _p: True)
    accept(store, window(), now=NOW, is_active_plan=lambda _p: True)
    assert store.latest("plan-demo")["window"]["end"] == "2026-11-08T05:31:20Z"


def test_clock_ahead_limit_matches_profile():
    assert MAX_CLOCK_AHEAD_SECONDS == PROFILE["max_clock_skew_seconds"]
    w = window(end="2026-11-08T05:31:23Z", start="2026-11-08T05:31:13Z")
    accept(SqliteWindowStore(), w, now=NOW, is_active_plan=lambda _p: True)
    with pytest.raises(IntakeRejected, match="clock_ahead"):
        accept(SqliteWindowStore(), window(end="2026-11-08T05:31:24Z", start="2026-11-08T05:31:14Z"), now=NOW, is_active_plan=lambda _p: True)


def test_plans_are_isolated():
    store = SqliteWindowStore()
    accept(store, window(), now=NOW, is_active_plan=lambda _p: True)
    accept(store, window(plan_id="plan-b"), now=NOW, is_active_plan=lambda _p: True)
    assert store.latest("plan-b")["plan_id"] == "plan-b"


def test_concurrent_same_window_is_stored_once(tmp_path):
    store = SqliteWindowStore(str(tmp_path / "w.db"))
    results = []
    barrier = threading.Barrier(8)

    def send():
        barrier.wait()
        try:
            accept(store, window(), now=NOW, is_active_plan=lambda _p: True)
            results.append("stored")
        except IntakeRejected as e:
            results.append(e.code)

    threads = [threading.Thread(target=send) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["duplicate"] * 7 + ["stored"]


def test_store_survives_reopen(tmp_path):
    path = str(tmp_path / "w.db")
    accept(SqliteWindowStore(path), window(), now=NOW, is_active_plan=lambda _p: True)
    with pytest.raises(IntakeRejected, match="duplicate"):
        accept(SqliteWindowStore(path), window(), now=NOW, is_active_plan=lambda _p: True)


def test_old_windows_are_pruned_but_replay_is_still_rejected():
    store = SqliteWindowStore()
    first = window(start="2026-11-08T05:31:10Z", end="2026-11-08T05:31:20Z")
    accept(store, first, now="2026-11-08T05:31:21Z", is_active_plan=lambda _p: True)
    accept(store, window(start="2026-11-08T06:40:00Z", end="2026-11-08T06:40:10Z"), now="2026-11-08T06:40:11Z", is_active_plan=lambda _p: True)
    assert store._db.execute("SELECT COUNT(*) FROM windows").fetchone() == (1,)
    with pytest.raises(IntakeRejected, match="out_of_order"):
        accept(store, first, now="2026-11-08T06:40:12Z", is_active_plan=lambda _p: True)


def test_contract_validation_reports_location_without_values():
    with pytest.raises(ContractViolation) as e:
        validate("SliWindow", {**window(), "plan_id": "Secret Value!"})
    assert "Secret Value" not in str(e.value) and "plan_id" in str(e.value)
