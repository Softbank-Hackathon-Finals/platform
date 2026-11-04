"""부하 실행기: 정해진 속도, 재시도, 멱등 키, 시도 기록을 본다. 가짜 서버(MockTransport)로 돌린다."""

import asyncio
import json
from collections import Counter

import httpx
import pytest

from bravo_loadgen.journal import Journal, read_journal
from bravo_loadgen.load import LoadConfig, LoadRunner, retry_delay

ENVS = {"aws": "source", "onprem": "target"}


def config(**overrides):
    base = dict(
        run_id="t18-test",
        plan_id="plan-demo",
        env_pools=ENVS,
        write_rps=10,
        read_rps=0,
        duration_s=0.1,
        window_seconds=1,
        attempt_timeout_s=1,
        retry_budget_s=5,
    )
    return LoadConfig(**{**base, **overrides})


def created(request, message_id=1, env="aws", status=201):
    return httpx.Response(status, json={"id": message_id}, headers={"X-Bravo-Env": env})


async def run(tmp_path, handler, sink=None, **overrides):
    path = tmp_path / "journal.jsonl"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://edge", timeout=None
    ) as client:
        with Journal(path) as journal:
            result = await LoadRunner(config(**overrides), client, journal, window_sink=sink).run()
    records = read_journal(path)
    return result, [r for r in records if r["type"] == "attempt"], records


def go(coroutine):
    return asyncio.run(coroutine)


def test_sends_at_the_configured_rate(tmp_path):
    calls = Counter()

    async def handler(request):
        calls[request.method] += 1
        return created(request) if request.method == "POST" else httpx.Response(200, json=[])

    result, attempts, _ = go(run(tmp_path, handler, write_rps=50, read_rps=150, duration_s=0.4))
    assert calls == {"POST": 20, "GET": 60}
    assert result.scheduled_writes == 20 and result.scheduled_reads == 60
    assert Counter(a["kind"] for a in attempts) == {"write": 20, "read": 60}


def test_open_loop_keeps_sending_while_the_server_is_slow(tmp_path):
    """서버가 느려져도 보내는 속도를 줄이지 않는다. 줄이면 장애 구간의 요청이 사라진다."""

    async def handler(request):
        await asyncio.sleep(0.5)
        return created(request)

    _, attempts, _ = go(run(tmp_path, handler, write_rps=50, duration_s=0.4))
    assert len(attempts) == 20
    assert max(a["sent_at_s"] for a in attempts) < 0.45  # 응답을 기다렸다면 10초가 걸린다


def test_retry_keeps_the_idempotency_key_and_body_with_a_new_attempt_id(tmp_path):
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(503, headers={"Retry-After": "0", "X-Bravo-Env": "aws"})
        return created(request)

    _, attempts, _ = go(run(tmp_path, handler, write_rps=10, duration_s=0.1))
    assert len(seen) == 2 and seen[0] == seen[1]
    key = seen[0]["idempotency_key"]
    assert [a["logical_id"] for a in attempts] == [key, key]
    assert [a["attempt_id"] for a in attempts] == [f"{key}.1", f"{key}.2"]
    assert [a["acked"] for a in attempts] == [False, True]
    assert [a["final"] for a in attempts] == [False, True]
    assert seen[0]["author"] == "loadgen-t18-test"
    assert seen[0]["body"] == f"t18 run=t18-test req={key}"


def test_retry_after_is_honored(tmp_path):
    count = Counter()

    async def handler(request):
        count["n"] += 1
        if count["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "1"})
        return created(request)

    _, attempts, _ = go(run(tmp_path, handler))
    assert attempts[1]["sent_at_s"] - attempts[0]["finished_at_s"] >= 0.99


def test_timeout_after_commit_recovers_the_ack_with_the_same_key(tmp_path):
    """응답만 유실된 경우: 재시도가 200(같은 메시지)을 받아 확인된다."""
    stored = {}

    async def handler(request):
        key = json.loads(request.content)["idempotency_key"]
        if key not in stored:
            stored[key] = 77
            await asyncio.sleep(1)
        return created(request, message_id=stored[key], status=200)

    _, attempts, _ = go(run(tmp_path, handler, attempt_timeout_s=0.05))
    assert [a["outcome"] for a in attempts] == ["timeout", "http"]
    assert attempts[0]["status"] is None and attempts[1]["message_id"] == 77


def test_connection_failure_is_retried(tmp_path):
    count = Counter()

    async def handler(request):
        count["n"] += 1
        if count["n"] == 1:
            raise httpx.ConnectError("refused")
        return created(request)

    _, attempts, _ = go(run(tmp_path, handler))
    assert [a["outcome"] for a in attempts] == ["connect", "http"]


def test_response_closed_midway_counts_as_connection_failure(tmp_path):
    count = Counter()

    async def handler(request):
        count["n"] += 1
        if count["n"] == 1:
            raise httpx.RemoteProtocolError("server disconnected")
        return created(request)

    _, attempts, _ = go(run(tmp_path, handler))
    assert attempts[0]["outcome"] == "connect" and attempts[1]["acked"]


@pytest.mark.parametrize("status", [409, 422])
def test_client_errors_are_final_and_not_retried(tmp_path, status):
    async def handler(request):
        return httpx.Response(status, headers={"X-Bravo-Env": "aws"})

    _, attempts, _ = go(run(tmp_path, handler))
    assert len(attempts) == 1 and attempts[0]["status"] == status
    assert attempts[0]["final"] is True and attempts[0]["acked"] is False


def test_success_without_a_message_id_is_not_an_ack(tmp_path):
    """Edge가 200 HTML을 돌려줘도 저장 확인이 아니다. 재시도해서 확인한다."""
    count = Counter()

    async def handler(request):
        count["n"] += 1
        if count["n"] == 1:
            return httpx.Response(200, text="<html>edge</html>")
        return created(request)

    _, attempts, _ = go(run(tmp_path, handler))
    assert [a["acked"] for a in attempts] == [False, True]


def test_undecodable_response_is_a_failed_attempt_not_a_crash(tmp_path):
    """깨진 응답 하나에 실행 전체가 무너지면 기록이 사라진다. 응답을 받지 못한 시도로 센다."""
    count = Counter()

    async def handler(request):
        count["n"] += 1
        if count["n"] == 1:
            raise httpx.DecodingError("bad gzip body")
        return created(request)

    result, attempts, records = go(run(tmp_path, handler))
    assert [a["outcome"] for a in attempts] == ["connect", "http"] and attempts[1]["acked"]
    assert records[-1]["type"] == "run_end"


def test_rate_limited_writes_are_retried(tmp_path):
    count = Counter()

    async def handler(request):
        count["n"] += 1
        if count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return created(request)

    _, attempts, _ = go(run(tmp_path, handler))
    assert [a["status"] for a in attempts] == [429, 201]


def test_attempt_timeout_shrinks_to_the_remaining_retry_budget(tmp_path):
    async def handler(request):
        await asyncio.sleep(5)
        return created(request)

    _, attempts, _ = go(run(tmp_path, handler, attempt_timeout_s=5, retry_budget_s=0.3))
    assert attempts[0]["outcome"] == "timeout" and attempts[0]["latency_ms"] < 500


def test_retry_budget_is_enforced(tmp_path):
    async def handler(request):
        return httpx.Response(503, headers={"Retry-After": "1"})

    _, attempts, _ = go(run(tmp_path, handler, retry_budget_s=0.2))
    assert len(attempts) == 1 and attempts[0]["final"] and not attempts[0]["acked"]


def test_max_attempts_is_enforced(tmp_path):
    async def handler(request):
        return httpx.Response(500)

    _, attempts, _ = go(run(tmp_path, handler, max_attempts=2, retry_budget_s=30))
    assert len(attempts) == 2 and attempts[-1]["final"]


def test_reads_poll_after_the_last_acked_id_and_are_not_retried(tmp_path):
    afters = []

    async def handler(request):
        if request.method == "POST":
            return created(request, message_id=41)
        afters.append(request.url.params.get("after"))
        return httpx.Response(503, headers={"X-Bravo-Env": "aws"})

    _, attempts, _ = go(run(tmp_path, handler, write_rps=20, read_rps=20, duration_s=0.5))
    reads = [a for a in attempts if a["kind"] == "read"]
    assert len(reads) == 10 and all(a["attempt_no"] == 1 and a["final"] for a in reads)
    assert afters[-1] == "41"


def test_read_timeouts_are_recorded(tmp_path):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=[])

    _, attempts, _ = go(run(tmp_path, handler, write_rps=0, read_rps=10, attempt_timeout_s=0.05))
    assert [a["outcome"] for a in attempts] == ["timeout"]


def test_windows_conserve_every_attempt_and_pass_the_contract(tmp_path):
    sent = []

    async def sink(window):
        sent.append(window)

    async def handler(request):
        if request.method == "POST":
            return created(request)
        return httpx.Response(200, json=[], headers={"X-Bravo-Env": "onprem"})

    result, attempts, _ = go(
        run(tmp_path, handler, sink=sink, write_rps=20, read_rps=40, duration_s=2.3, window_seconds=1)
    )
    assert len(result.windows) == 2 and sent == result.windows
    counted = sum(
        w["pools"]["source"]["requests"] + w["pools"]["target"]["requests"] for w in result.windows
    )
    assert counted + result.tail_requests + result.late_requests == len(attempts)
    assert result.late_requests == 0 and result.invalid_windows == 0
    assert result.continuity_problems == []
    assert sum(w["pools"]["source"]["requests"] for w in result.windows) > 0
    assert sum(w["pools"]["target"]["requests"] for w in result.windows) > 0


def test_the_last_complete_window_is_emitted_when_load_ends_on_a_boundary(tmp_path):
    """마지막 요청은 경계 직전에 끝난다. 그래도 부하 시간이 창의 배수면 마지막 창까지 나와야 한다."""

    async def handler(request):
        return created(request)

    result, attempts, _ = go(run(tmp_path, handler, write_rps=10, duration_s=2, window_seconds=1))
    assert [w["pools"]["source"]["requests"] for w in result.windows] == [10, 10]
    assert result.tail_requests == 0 and len(attempts) == 20


def test_every_response_kind_reaches_the_window_in_its_contract_bucket(tmp_path):
    """실제 응답이 Record로 옮겨져 계약의 칸에 세어지는지 끝까지 본다."""
    count = Counter()

    async def handler(request):
        count["n"] += 1
        kind = count["n"] % 6
        if kind == 0:
            return httpx.Response(200, json=[], headers={"X-Bravo-Env": "aws"})
        if kind == 1:
            return httpx.Response(503, headers={"X-Bravo-Env": "aws"})
        if kind == 2:
            return httpx.Response(409, headers={"X-Bravo-Env": "onprem"})
        if kind == 3:
            return httpx.Response(200, json=[])
        if kind == 4:
            await asyncio.sleep(1)
        raise httpx.ConnectError("refused")

    result, attempts, _ = go(
        run(tmp_path, handler, write_rps=0, read_rps=30, duration_s=1, window_seconds=1,
            attempt_timeout_s=0.05)
    )
    window = result.windows[0]
    assert (window["pools"]["source"]["requests"], window["pools"]["source"]["http_5xx"]) == (10, 5)
    assert window["pools"]["source"]["p95_ms"] is not None
    assert (window["pools"]["target"]["requests"], window["pools"]["target"]["integrity_conflict"]) == (5, 5)
    # 시간 초과 요청은 부하 끝보다 늦게 끝나 일부가 꼬리로 갈 수 있다. 합계로 맞춘다.
    assert window["unattributed"]["other"] == 5 and window["unattributed"]["connect"] == 5
    assert window["unattributed"]["timeout"] + result.tail_requests == 5


def test_a_partial_tail_is_not_emitted_as_a_window(tmp_path):
    """계약은 창 길이가 정확히 같기를 요구한다(WINDOW_MISMATCH). 남은 요청은 따로 센다."""

    async def handler(request):
        return created(request)

    result, attempts, _ = go(run(tmp_path, handler, duration_s=0.5, window_seconds=1))
    assert result.windows == []
    assert result.tail_requests == len(attempts)


def test_window_delivery_failure_is_counted_and_load_continues(tmp_path):
    async def sink(window):
        raise httpx.ConnectError("platform down")

    async def handler(request):
        return created(request)

    result, attempts, _ = go(run(tmp_path, handler, sink=sink, duration_s=1.2, window_seconds=1))
    assert result.delivery_failed == 1 and len(result.windows) == 1
    assert len(attempts) == 12


def test_stalled_period_is_reported_as_an_empty_window_not_skipped(tmp_path):
    """앞 구간에 응답이 하나도 안 오면 빈 창이 나간다. 판정은 표본 부족으로 멈춘다."""

    async def handler(request):
        await asyncio.sleep(1.2)
        return created(request)

    result, _, _ = go(run(tmp_path, handler, write_rps=10, duration_s=0.5, window_seconds=1,
                          attempt_timeout_s=2))
    assert result.windows[0]["pools"]["source"] == {
        "requests": 0, "http_5xx": 0, "integrity_conflict": 0, "p95_ms": None,
    }


def test_journal_marks_a_completed_run(tmp_path):
    async def handler(request):
        return created(request)

    _, _, records = go(run(tmp_path, handler))
    assert records[0]["type"] == "run_start" and records[0]["run_id"] == "t18-test"
    assert records[-1]["type"] == "run_end" and records[-1]["scheduled_writes"] == 1


@pytest.mark.parametrize(
    ("header", "attempt", "expected"),
    [
        ("2", 0, 2.0),
        ("0", 0, 0.5),
        ("0.1", 0, 0.5),  # 하한
        ("3600", 0, 30.0),  # 상한
        (None, 0, 0.5),
        (None, 3, 4.0),
        (None, 9, 5.0),
        ("not-a-number", 1, 1.0),
    ],
)
def test_retry_delay_matches_the_browser_client(header, attempt, expected):
    """demo-chat app/static/transport.js의 retryDelay와 같은 규칙이다(실제 사용자와 같은 재시도)."""
    assert retry_delay(header, attempt) == expected


@pytest.mark.parametrize(
    "bad",
    [
        {"run_id": "x" * 33},
        {"run_id": "Bad Id"},
        {"plan_id": "bad plan!"},
        {"env_pools": {"aws": "source", "onprem": "source"}},
        {"window_seconds": 0},
        {"duration_s": 0},
        {"write_rps": -1},
    ],
)
def test_invalid_configuration_is_rejected_before_any_request(bad):
    with pytest.raises(ValueError):
        config(**bad)
