#!/usr/bin/env python3
"""demo-chat의 Store를 직접 불러 쓰기 잠금을 재고 불변식을 시험한다. 로컬 시험 전용이다.

개선안마다 고친 store.py를 시험하므로 그 demo-chat의 인터프리터로 돈다(lock_lab.py가 실행한다).

    <demo-chat>/.venv/bin/python lock_lab_driver.py <명령> < 설정 JSON

설정과 비밀번호는 표준 입력으로만 받는다. 결과는 표준 출력에 JSON 한 줄이다.
- one-write: 쓰기 한 건씩 보낸다. 왕복은 lock_lab_tap.py의 기록으로 센다.
- ceiling: 동시 쓰기를 쉬지 않고 넣어 초당 처리량의 천장을 잰다.
- invariants: 커밋 순서 · 건너뛰기 · 멱등 · 없는 첨부 · 시간 초과 · 연결 끊김 · 세션 설정 누수를 시험한다.
"""

import asyncio
import json
import sys
import time
import uuid

import psycopg
from app.store import MESSAGE_WRITE_LOCK, IdempotencyConflict, Store, UnknownAttachment
from psycopg import OperationalError
from psycopg_pool import AsyncConnectionPool, PoolTimeout

TX_TIMEOUT_MS = 200
RESPONSE_FIELDS = {
    "id", "author", "body", "created_at", "attachment_id", "attachment_name", "attachment_type", "attachment_size",
}  # fmt: skip


def app_pool(dsn: str, max_size: int = 10) -> AsyncConnectionPool:
    """demo-chat app/main.py와 같은 설정이다."""
    return AsyncConnectionPool(
        dsn, kwargs={"prepare_threshold": None}, min_size=1, max_size=max_size, timeout=5, open=False
    )


def admin(config: dict) -> psycopg.AsyncConnection:
    return psycopg.AsyncConnection.connect(config["admin_dsn"], autocommit=True, prepare_threshold=None)


async def one_write(config: dict) -> dict:
    """왕복 수 측정용. 새로 쓰기 · 재전송 · 내용 충돌 · 없는 첨부를 한 건씩 보낸다. 사이에 표시를 둘 시간을 준다."""
    results = {}
    async with app_pool(config["dsn"], max_size=1) as pool:
        await pool.wait()
        store = Store(pool, TX_TIMEOUT_MS)
        await asyncio.sleep(0.3)
        key = uuid.uuid4()
        results["fresh"] = await _timed(store, key, "tap", "first", None)
        await asyncio.sleep(0.3)
        results["replay"] = await _timed(store, key, "tap", "first", None)
        await asyncio.sleep(0.3)
        results["conflict"] = await _timed(store, key, "tap", "different", None)
        await asyncio.sleep(0.3)
        results["unknown_attachment"] = await _timed(store, uuid.uuid4(), "tap", "x", uuid.uuid4())
        await asyncio.sleep(0.3)
    return results


async def _timed(store, key, author, body, attachment) -> dict:
    started = time.monotonic()
    outcome = "ok"
    try:
        await store.add_message(key, author, body, attachment)
    except (IdempotencyConflict, UnknownAttachment) as e:
        outcome = type(e).__name__
    return {
        "outcome": outcome,
        "started_ms": round(started * 1000, 3),
        "ended_ms": round(time.monotonic() * 1000, 3),
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }


async def ceiling(config: dict) -> dict:
    """동시 쓰기 작업을 쉬지 않고 돌린다. 처음 몇 초는 빼고 센다."""
    duration, warmup, workers = config["duration"], config["warmup"], config["workers"]
    counts = {"ok": 0, "timeout": 0, "pool": 0}
    latencies: list[float] = []
    async with app_pool(config["dsn"]) as pool:
        await pool.wait()
        store = Store(pool, config.get("tx_timeout_ms", TX_TIMEOUT_MS))
        started = time.monotonic()
        measure_from = started + warmup
        stop_at = started + warmup + duration

        async def worker():
            while (now := time.monotonic()) < stop_at:
                try:
                    await store.add_message(uuid.uuid4(), "ceiling", "x", None)
                    kind = "ok"
                except PoolTimeout:
                    kind = "pool"
                except OperationalError:
                    kind = "timeout"
                if now >= measure_from:
                    counts[kind] += 1
                    if kind == "ok":
                        latencies.append(time.monotonic() - now)

        await asyncio.gather(*(worker() for _ in range(workers)))
    latencies.sort()
    return {
        **counts,
        "tx_per_s": round(counts["ok"] / duration, 2),
        "latency_p50_ms": round(latencies[len(latencies) // 2] * 1000, 2) if latencies else None,
    }


async def send_retrying(store: Store, key, author, body, attachment, *, failures: list | None = None):
    """클라이언트처럼 일시 오류(앱에서는 503)면 같은 키로 다시 보낸다."""
    while True:
        try:
            return await store.add_message(key, author, body, attachment)
        except OperationalError as e:
            if failures is not None:
                failures.append(type(e).__name__)
            await asyncio.sleep(0.02)


class Checks:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, invariant: str, name: str, ok: bool, actual) -> None:
        self.items.append({"invariant": invariant, "check": name, "ok": bool(ok), "actual": actual})


async def check_idempotency(store: Store, far: Store, a: Checks, config: dict) -> None:
    key = uuid.uuid4()
    first, created = await store.add_message(key, "a", "hello", None)
    again, created_again = await far.add_message(key, "a", "hello", None)
    a.add("idempotent", "새 쓰기는 생성, 같은 키 재전송은 기존 메시지", (created, created_again) == (True, False), [created, created_again])
    a.add("idempotent", "재전송이 같은 행을 돌려줌", first == again, first == again)
    a.add("idempotent", "응답 필드가 원본과 같음", set(first) == RESPONSE_FIELDS and set(again) == RESPONSE_FIELDS, sorted(first))

    outcomes = []
    for author, body, attachment in (("a", "changed", None), ("b", "hello", None), ("a", "hello", uuid.uuid4())):
        try:
            await store.add_message(key, author, body, attachment)
            outcomes.append("accepted")
        except IdempotencyConflict:
            outcomes.append("conflict")
        except UnknownAttachment:
            outcomes.append("unknown_attachment")
    a.add("idempotent", "같은 키에 다른 내용이면 충돌", outcomes == ["conflict"] * 3, outcomes)

    # 두 경로에서 같은 키로 동시에 보낸다.
    key = uuid.uuid4()
    both = await asyncio.gather(*(send_retrying(s, key, "c", "same", None) for s in (store, far) * 10))
    made = sum(created for _, created in both)
    ids = {m["id"] for m, _ in both}
    async with await admin(config) as conn:
        rows = (await (await conn.execute("SELECT count(*) FROM messages WHERE idempotency_key = %s", (key,))).fetchone())[0]
    a.add("idempotent", "동시 같은 키 20건: 생성 1, 행 1, id 1", (made, rows, len(ids)) == (1, 1, 1), [made, rows, len(ids)])

    # 내용이 다른 동시 재전송: 하나만 이기고 나머지는 재전송이거나 충돌이다.
    key = uuid.uuid4()
    mixed = await asyncio.gather(
        *(send_retrying(s, key, "c", str(i % 2), None) for i, s in enumerate((store, far) * 5)), return_exceptions=True
    )
    won = [m for m in mixed if isinstance(m, tuple) and m[1]]
    wrong = [m for m in mixed if isinstance(m, BaseException) and not isinstance(m, IdempotencyConflict)]
    conflicts = sum(isinstance(m, IdempotencyConflict) for m in mixed)
    a.add("idempotent", "내용이 다른 동시 재전송: 생성 1, 충돌 5, 다른 예외 0", (len(won), conflicts, len(wrong)) == (1, 5, 0), [len(won), conflicts, [repr(w) for w in wrong]])


async def check_attachment(store: Store, config: dict, a: Checks) -> None:
    missing_key = uuid.uuid4()
    outcome = None
    try:
        await store.add_message(missing_key, "a", "x", uuid.uuid4())
    except UnknownAttachment:
        outcome = "unknown_attachment"
    except Exception as e:  # noqa: BLE001 - 어떤 예외든 그대로 시험 결과로 남긴다
        outcome = type(e).__name__
    a.add("attachment", "없는 첨부는 UnknownAttachment", outcome == "unknown_attachment", outcome)

    async with await admin(config) as conn:
        rows = (await (await conn.execute("SELECT count(*) FROM messages WHERE idempotency_key = %s", (missing_key,))).fetchone())[0]
        held = (await (await conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s", (MESSAGE_WRITE_LOCK,))).fetchone())[0]
    a.add("attachment", "실패한 쓰기의 행이 남지 않음", rows == 0, rows)
    a.add("attachment", "실패 뒤 잠금이 남지 않음", held == 0, held)

    # 크기 1인 풀이라 같은 연결을 다시 쓴다. 망가진 상태로 돌아오면 여기서 드러난다.
    message, created = await store.add_message(missing_key, "a", "x", None)
    a.add("attachment", "같은 연결 · 같은 키로 다시 보내면 정상 저장", created and message["attachment_id"] is None, created)

    attachment = uuid.uuid4()
    await store.add_attachment(attachment, f"attachments/{attachment}", "a.png", "image/png", 3)
    message, created = await store.add_message(uuid.uuid4(), "a", "", attachment)
    a.add(
        "attachment",
        "있는 첨부는 이름 · 형식 · 크기가 응답에 실림",
        created and (message["attachment_name"], message["attachment_type"], message["attachment_size"]) == ("a.png", "image/png", 3),
        [message["attachment_name"], message["attachment_type"], message["attachment_size"]],
    )
    key = uuid.uuid4()
    await store.add_message(key, "a", "", attachment)
    again, created = await store.add_message(key, "a", "", attachment)
    a.add("attachment", "첨부 있는 메시지의 재전송도 같은 응답", not created and again["attachment_name"] == "a.png", again["attachment_name"])


async def attempt_with_deadline(store: Store, key, author, body) -> str:
    """앱의 시간 제한이 듣지 않으면 여기서 영원히 기다리게 된다. 그것도 결함이므로 시험이 끊어 기록한다."""
    try:
        await asyncio.wait_for(store.add_message(key, author, body, None), timeout=3)
        return "accepted"
    except OperationalError as e:
        return type(e).__name__
    except TimeoutError:
        return "no_timeout_in_3s"


async def check_timeout(store: Store, config: dict, a: Checks) -> None:
    """누가 잠금을 오래 쥐면 정해진 시간 안에 포기하고 연결은 멀쩡해야 한다. 503 + Retry-After의 근거다."""
    key = uuid.uuid4()
    async with await admin(config) as holder:
        async with holder.transaction():
            await holder.execute("SELECT pg_advisory_xact_lock(%s)", (MESSAGE_WRITE_LOCK,))
            started = time.monotonic()
            outcome = None
            outcome = await attempt_with_deadline(store, key, "a", "blocked")
            waited = (time.monotonic() - started) * 1000
    a.add("timeout", "잠금 대기 초과는 OperationalError(앱이 503으로 바꾼다)", outcome in ("QueryCanceled", "LockNotAvailable"), outcome)
    limit = TX_TIMEOUT_MS + 8 * config.get("rtt_ms", 0) + 300
    a.add("timeout", f"정해진 시간 안에 포기 (< {limit}ms)", waited < limit, round(waited, 1))
    message, created = await store.add_message(key, "a", "blocked", None)
    a.add("timeout", "잠금이 풀리면 같은 키로 저장", created and message["body"] == "blocked", created)

    # 다른 트랜잭션이 같은 키를 쓰는 중이면 ON CONFLICT가 그 끝을 기다린다. 이 대기도 묶여야 한다.
    key = uuid.uuid4()
    async with await admin(config) as holder:
        async with holder.transaction():
            await holder.execute(
                "INSERT INTO messages (idempotency_key, author, body) VALUES (%s, 'holder', 'x')", (key,)
            )
            started = time.monotonic()
            outcome = None
            outcome = await attempt_with_deadline(store, key, "holder", "x")
            waited = (time.monotonic() - started) * 1000
    a.add("timeout", "같은 키 대기도 시간 안에 포기", outcome in ("QueryCanceled", "LockNotAvailable") and waited < limit, [outcome, round(waited, 1)])


async def check_session_state(store: Store, config: dict, a: Checks) -> None:
    """트랜잭션 범위 설정이 세션에 남으면 PgBouncer transaction 모드에서 다른 앱의 연결로 샌다."""
    await store.add_message(uuid.uuid4(), "a", "x", None)
    async with store.pool.connection() as conn:
        statement = (await (await conn.execute("SHOW statement_timeout")).fetchone())[0]
        lock = (await (await conn.execute("SHOW lock_timeout")).fetchone())[0]
        autocommit = conn.autocommit
    a.add("session", "쓰기 뒤 statement_timeout · lock_timeout이 세션에 남지 않음", statement == "0" and lock == "0", [statement, lock])
    a.add("session", "연결의 자동 커밋 설정이 원래대로 돌아옴", autocommit is False, autocommit)


async def check_order(store: Store, far: Store, config: dict, a: Checks) -> None:
    """커밋 순서는 논리 디코딩으로, 건너뛰기는 앱과 같은 조회로 본다. 두 경로에서 동시에 쓴다."""
    writes = config["order_writes"]
    slot = f"lock_lab_{uuid.uuid4().hex[:8]}"
    async with await admin(config) as conn:
        await conn.execute("SELECT pg_create_logical_replication_slot(%s, 'test_decoding')", (slot,))
        start = (await (await conn.execute("SELECT coalesce(max(id), 0) FROM messages")).fetchone())[0]
    try:
        seen: list[list[int]] = [[] for _ in range(4)]
        done = asyncio.Event()

        async def poller(index: int):
            last = start
            while True:
                finishing = done.is_set()
                items = await store.list_messages(last, 200)
                seen[index].extend(m["id"] for m in items)
                if items:
                    last = items[-1]["id"]
                elif finishing:
                    return

        failures: list[str] = []
        queue = iter(range(writes))

        async def writer(target: Store):
            for i in queue:
                # 일부는 재전송이다. 재전송은 id를 태워 빈 번호를 만든다.
                key = uuid.uuid4()
                for _ in range(1 + (i % 10 == 0)):
                    await send_retrying(target, key, "order", str(i), None, failures=failures)

        pollers = [asyncio.create_task(poller(i)) for i in range(len(seen))]
        await asyncio.gather(*(writer(store if i % 2 else far) for i in range(config.get("order_writers", 10))))
        done.set()
        await asyncio.gather(*pollers)

        async with await admin(config) as conn:
            rows = await (await conn.execute("SELECT id FROM messages WHERE id > %s AND author = 'order' ORDER BY id", (start,))).fetchall()
            changes = await (await conn.execute("SELECT data FROM pg_logical_slot_get_changes(%s, NULL, NULL)", (slot,))).fetchall()
    finally:
        async with await admin(config) as conn:
            await conn.execute("SELECT pg_drop_replication_slot(%s)", (slot,))

    stored = [row[0] for row in rows]
    decoded = []
    for (line,) in changes:
        if line.startswith("table public.messages: INSERT:") and "author[text]:'order'" in line:
            decoded.append(int(line.split("id[bigint]:")[1].split()[0]))
    inversions = sum(1 for x, y in zip(decoded, decoded[1:], strict=False) if y <= x)
    a.add("order", "쓰기가 모두 저장됨", len(stored) == writes, {"rows": len(stored), "timeouts_retried": len(failures)})
    a.add("order", "논리 디코딩에 모든 행이 나옴", sorted(decoded) == stored, [len(decoded), len(stored)])
    a.add("order", "커밋 순서와 id 순서 일치 (뒤집힘 0)", inversions == 0, inversions)
    # 빈 조회가 끝을 뜻하므로 작성자가 다른 행도 섞일 수 있어 이 실행의 행으로 좁힌다.
    wanted = set(stored)
    skipped = [len(wanted - set(ids)) for ids in seen]
    a.add("skip", "4개 조회 모두 건너뛴 메시지 0", skipped == [0, 0, 0, 0], skipped)
    ordered = all(ids == sorted(ids) for ids in seen)
    a.add("skip", "4개 조회 모두 id 오름차순", ordered, ordered)


async def check_connection_loss(config: dict, a: Checks) -> None:
    """앱-DB 연결이 응답 중에 끊겨도 같은 키 재시도로 정확히 한 행만 남아야 한다."""
    dsn = config.get("flaky_dsn")
    if not dsn:
        return
    keys = [uuid.uuid4() for _ in range(config.get("flaky_writes", 120))]
    errors: dict[str, int] = {}
    async with AsyncConnectionPool(
        dsn, kwargs={"prepare_threshold": None}, min_size=1, max_size=4, timeout=5, reconnect_timeout=2, open=False
    ) as pool:
        store = Store(pool, TX_TIMEOUT_MS)

        async def send(key: uuid.UUID) -> bool | None:
            """확인을 받으면 새로 저장됐는지를 돌려준다. False면 앞 시도가 저장했는데 응답을 못 받은 것이다."""
            for _ in range(60):
                try:
                    _, created = await store.add_message(key, "flaky", str(key), None)
                    return created
                except OperationalError as e:
                    errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
                except Exception as e:  # noqa: BLE001 - 503이 아닌 예외(500)는 결함이다
                    name = f"unexpected:{type(e).__name__}"
                    errors[name] = errors.get(name, 0) + 1
                await asyncio.sleep(0.05)
            return None

        results = [await send(key) for key in keys]
    async with await admin(config) as conn:
        rows = await (await conn.execute("SELECT idempotency_key, count(*) FROM messages WHERE idempotency_key = ANY(%s) GROUP BY 1", (keys,))).fetchall()
        held = (await (await conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s", (MESSAGE_WRITE_LOCK,))).fetchone())[0]
    unexpected = {k: v for k, v in errors.items() if k.startswith("unexpected")}
    a.add("connection-loss", "연결이 실제로 끊김", sum(errors.values()) > 0, errors)
    ack_lost = sum(result is False for result in results)
    a.add("connection-loss", "커밋 뒤 응답 유실이 실제로 일어남 (재시도가 저장된 행을 돌려받음)", ack_lost > 0, ack_lost)
    a.add("connection-loss", "끊겨도 500이 될 예외 없음", not unexpected, unexpected)
    a.add("connection-loss", "재시도로 전부 확인", all(result is not None for result in results), sum(result is not None for result in results))
    a.add("connection-loss", "키마다 정확히 한 행", len(rows) == len(keys) and all(n == 1 for _, n in rows), [len(rows), len(keys)])
    a.add("connection-loss", "끊긴 뒤 잠금이 남지 않음", held == 0, held)


async def invariants(config: dict) -> dict:
    a = Checks()
    async with app_pool(config["dsn"], max_size=1) as one, app_pool(config["dsn"]) as near, app_pool(config["far_dsn"]) as far_pool:
        await asyncio.gather(one.wait(), near.wait(), far_pool.wait())
        single, store, far = Store(one, TX_TIMEOUT_MS), Store(near, TX_TIMEOUT_MS), Store(far_pool, TX_TIMEOUT_MS)
        await check_idempotency(store, far, a, config)
        await check_attachment(single, config, a)
        await check_timeout(single, config, a)
        await check_session_state(single, config, a)
        await check_order(store, far, config, a)
    await check_connection_loss(config, a)
    return {"checks": a.items}


COMMANDS = {"one-write": one_write, "ceiling": ceiling, "invariants": invariants}

if __name__ == "__main__":
    print(json.dumps(asyncio.run(COMMANDS[sys.argv[1]](json.load(sys.stdin))), ensure_ascii=False))
