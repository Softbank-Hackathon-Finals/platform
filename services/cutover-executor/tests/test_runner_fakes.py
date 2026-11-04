"""원격 대상을 가짜로 둔 실행기 시험 (CI에서도 돈다. 실행 기록은 docker Postgres).

#116 리뷰(류지환) 3건의 회귀 시험:
- 단계 안에서 소유를 잃으면 그다음 원격 조작과 실행 기록을 하지 않는다
- ConfigMap 영구 반영 실패 · 충돌은 DONE이 아니라 SAFE_HOLD(lease 유지)
- PgBouncer 파드가 Ready가 아니거나 실행 도중 바뀌면 상태 불명으로 본다
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from bravo_cutover import runner as runner_module
from bravo_cutover.contract import CutoverRequest, Status
from bravo_cutover.db_fingerprint import SequenceState
from bravo_cutover.remote import Unconfirmed
from bravo_cutover.remote.db import Subscription, WriteRefusal
from bravo_cutover.remote.pgbouncer import PoolState
from bravo_cutover.runner import STEPS, Cutover
from bravo_cutover.sites import Config, Settings, Site, registry_from_sites

INI = "[databases]\nchat = host={host} port=5432 dbname=chat\n\n[pgbouncer]\nlisten_port = 6432\n"


def site(env: str, ns: str, subscription: str) -> Site:
    return Site(
        environment_id=env, resource_id=f"fake-{ns}/chat", connection_ref=f"fake/{ns}",
        kube_context="fake", namespace=ns, subscription=subscription,
        pgbouncer_admin_dsn=f"fake:{ns}:admin", pgbouncer_app_dsn=f"fake:{ns}:pool",
        db_control_dsn=f"fake:{ns}:control", db_app_dsn=f"fake:{ns}:db", db_host=f"{ns}-db",
    )


CONFIG = Config(
    settings=Settings(restore_verify_timeout_s=1),
    sites={"env-aws": site("env-aws", "src", "bravo_rev"), "env-onprem": site("env-onprem", "dst", "bravo_fwd")},
    origin="env-aws",
)


class World:
    """두 환경의 상태와, 원격 상태를 바꾼 호출의 기록(calls)."""

    def __init__(self):
        self.login = {"src": True, "dst": False}
        self.sub = {"src": False, "dst": True}  # src: 역방향, dst: 정방향
        self.paused = {"src": False, "dst": False}
        self.ini_host = {"src": "src-db", "dst": "src-db"}
        self.blocked = {"src": False, "dst": False}
        self.pod = {"src": {"name": "pgb-src", "uid": "u-src", "restarts": 0, "ready": True, "terminating": False},
                    "dst": {"name": "pgb-dst", "uid": "u-dst", "restarts": 0, "ready": True, "terminating": False}}
        self.configmap = {ns: (INI.format(host="src-db"), "1") for ns in ("src", "dst")}
        self.configmap_error: dict[str, Exception] = {}
        self.barriers: set = set()
        self.path_down = False  # 복제 경로가 끊겼다: 사전 확인의 시험 행이 목적지에 오지 않는다
        self.sessions = {"src": 0, "dst": 0}  # 앱 역할 세션(NOLOGIN 전에 붙은 연결 포함)
        self.bypass = {"src": [], "dst": []}  # 업무 역할이 아닌 클라이언트 세션
        self.seq = {"src": SequenceState(9, True, 9, 2**63 - 1), "dst": SequenceState(9, True, 9, 2**63 - 1)}
        self.seq_readback_lost = False  # 목적지 시퀀스를 올려도 다시 읽으면 그대로(올리기 미반영)
        self.on_sequence_read: dict[str, object] = {}  # 환경 → 그 환경 시퀀스를 처음 읽을 때 실행할 함수
        self.calls: list[str] = []
        self.after: dict[str, object] = {}  # 호출 이름 → 그 호출 직후 실행할 함수

    def did(self, name: str) -> None:
        self.calls.append(name)
        if fn := self.after.pop(name, None):
            fn()

    def writer_of(self, ns: str) -> str:
        return "src" if self.ini_host[ns] == "src-db" else "dst"


class FakeDb:
    def __init__(self, w: World, ns: str):
        self.w, self.ns = w, ns

    def can_login(self, role):
        return self.w.login[self.ns]

    def set_login(self, role, allowed):
        self.w.login[self.ns] = allowed
        self.w.did(f"{self.ns}.set_login:{allowed}")

    def sessions(self, role):
        return self.w.sessions[self.ns]

    def terminate_sessions(self, role):
        self.w.did(f"{self.ns}.terminate_sessions")
        return 0

    def long_transactions(self, role, older_than_ms):
        return 0

    def other_sessions(self, roles):
        return list(self.w.bypass[self.ns])

    def subscription(self, name):
        return Subscription(enabled=self.w.sub[self.ns], workers=1 if self.w.sub[self.ns] else 0)

    def set_subscription(self, name, enabled, owner, wait_s=5):
        self.w.sub[self.ns] = enabled
        self.w.did(f"{self.ns}.set_subscription:{enabled}")

    def received_lsn(self, subscription):
        return 100

    def current_wal_lsn(self):
        return 100

    def insert_barrier(self, barrier_id, epoch):
        self.w.barriers.add(barrier_id)
        self.w.did(f"{self.ns}.insert_barrier")

    def insert_path_probe(self, probe_id, epoch):
        if not self.w.path_down:
            self.w.barriers.add(probe_id)
        self.w.did(f"{self.ns}.insert_path_probe")

    def has_barrier(self, barrier_id):
        return barrier_id in self.w.barriers

    def last_msg_receipt_time(self, subscription):
        return "2026-11-04T00:00:00+00:00"

    def sequence_state(self, sequence, table, column):
        if fn := self.w.on_sequence_read.pop(self.ns, None):
            fn()
        return self.w.seq[self.ns]

    def raise_sequence(self, sequence, next_value):
        self.w.did(f"{self.ns}.raise_sequence")
        current = self.w.seq[self.ns]
        if not self.w.seq_readback_lost and next_value > current.next_value:
            self.w.seq[self.ns] = SequenceState(next_value, False, current.max_id, current.maximum)
        return max(current.next_value, next_value)


class FakePool:
    def __init__(self, w: World, ns: str):
        self.w, self.ns = w, ns

    def state(self):
        return PoolState(paused=self.w.paused[self.ns], host=self.w.ini_host[self.ns])

    def pause(self, timeout_s):
        self.w.paused[self.ns] = True
        self.w.did(f"{self.ns}.pause")

    def resume(self):
        self.w.paused[self.ns] = False
        self.w.did(f"{self.ns}.resume")

    def reload(self):
        self.w.did(f"{self.ns}.reload")


class FakePoolConfig:
    def __init__(self, w: World, ns: str):
        self.w, self.ns = w, ns

    def identity(self):
        pod = self.w.pod[self.ns]
        if not pod["ready"]:
            raise Unconfirmed(f"pgbouncer pod {pod['name']} is not ready")
        return pod["name"], pod["uid"], pod["restarts"]

    def retarget(self, database, host, port, dbname, pod=None):
        self.w.ini_host[self.ns] = host
        self.w.did(f"{self.ns}.retarget:{host}")


class FakeEntry:
    def __init__(self, w: World, ns: str):
        self.w, self.ns = w, ns

    def set(self, blocked):
        self.w.blocked[self.ns] = blocked
        self.w.did(f"{self.ns}.entry:{blocked}")
        return {"app-0": blocked}

    def get(self):
        return {"app-0": self.w.blocked[self.ns]}

    def running(self):
        return ["app-0"]

    def identities(self):
        return [{"name": "app-0", "uid": "app-uid-" + self.ns, "restarts": 0,
                 "ready": True, "terminating": False}]

    def close_websockets(self):
        self.w.did(f"{self.ns}.ws:1012")
        return {"app-0": 1}


class FakeKube:
    def __init__(self, w: World, ns: str):
        self.w, self.ns = w, ns

    def read_configmap_versioned(self, namespace, name, key):
        return self.w.configmap[self.ns]

    def read_configmap(self, namespace, name, key):
        return self.w.configmap[self.ns][0]

    def patch_configmap(self, namespace, name, key, value, resource_version=None):
        self.w.did(f"{self.ns}.patch_configmap")
        if error := self.w.configmap_error.get(self.ns):
            raise error
        current, version = self.w.configmap[self.ns]
        if resource_version is not None and resource_version != version:
            raise Unconfirmed(f"patch configmap: conflict (resourceVersion {resource_version} != {version})")
        self.w.configmap[self.ns] = (value, str(int(version) + 1))


class FixedComparer:
    def __init__(self, result):
        self.result = result

    def compare(self, src, dst, tables):
        return self.result


# T13 계약 「결과 모양」과 같은 칸. 지문 값은 실행기가 해석하지 않으므로 아무 값이나 된다.
TABLES = [{"name": t, "rows_src": 5, "rows_dst": 5, "source": {"rows": 5}, "target": {"rows": 5},
           "match": True, "data_verdict": "MATCH", "schema_verdict": "MATCH"} for t in CONFIG.settings.tables]
MATCH = {"verdict": "MATCH", "data_verdict": "MATCH", "schema_verdict": "MATCH", "hash_rule": "v1", "tables": TABLES}


def match_with(table: int | None = None, drop: tuple[str, ...] = (), **fields):
    """MATCH 결과에서 한 곳만 바꾼다. table을 주면 그 표의 칸을, 아니면 전체 칸을 바꾸거나 뺀다."""
    result = {**MATCH, "tables": [dict(t) for t in TABLES]}
    target = result if table is None else result["tables"][table]
    target.update(fields)
    for key in drop:
        target.pop(key)
    return result


class FakeCutover(Cutover):
    """비교는 기본으로 MATCH를 돌려준다. 실제 DB 지문 비교는 test_db_fingerprint_runner가 잰다."""

    def __init__(self, store, world: World, **kwargs):
        kwargs.setdefault("comparer", FixedComparer(MATCH))
        super().__init__(store, CONFIG, kube=lambda context: None, **kwargs)
        self.world = world

    def _side(self, environment_id):
        s = CONFIG.sites[environment_id]
        ns, w = s.namespace, self.world
        return SimpleNamespace(site=s, kube=FakeKube(w, ns), db=FakeDb(w, ns), pool=FakePool(w, ns),
                               pool_config=FakePoolConfig(w, ns), entry=FakeEntry(w, ns))


@pytest.fixture
def world(monkeypatch):
    w = World()

    def try_app_write(dsn, path, timeout_s=3):
        _, ns, kind = dsn.split(":")
        if kind == "db":
            return WriteRefusal.ACCEPTED if w.login[ns] else WriteRefusal.REFUSED
        writer = w.writer_of(ns)
        return WriteRefusal.ACCEPTED if not w.paused[ns] and w.login[writer] else WriteRefusal.REFUSED

    def app_write(dsn, path, timeout_s=3):
        if try_app_write(dsn, path) != WriteRefusal.ACCEPTED:
            raise RuntimeError("write refused")
        w.did(f"{dsn.split(':')[1]}.probe")
        return 0.001

    monkeypatch.setattr(runner_module, "try_app_write", try_app_write)
    monkeypatch.setattr(runner_module, "app_write", app_write)
    return w


REFS = {"env-aws": "fake/src", "env-onprem": "fake/dst"}


def start(store, source: str = "env-aws", target: str = "env-onprem",
          deadline: timedelta = timedelta(minutes=1)) -> uuid.UUID:
    request = CutoverRequest(
        plan_id="plan-fake", epoch=int(datetime.now(UTC).timestamp() * 1e6),
        source={"environment_id": source, "connection_ref": REFS[source]},
        target={"environment_id": target, "connection_ref": REFS[target]},
        write_pause_budget_ms=5000, deadline=datetime.now(UTC) + deadline,
    )
    return store.create(str(uuid.uuid4()), request, registry_from_sites(CONFIG.sites).resolve(request))[0].execution_id


def test_happy_path_with_fakes(store, world):
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result
    assert set(execution.result["stages_ms"]) == set(STEPS)
    assert world.login == {"src": False, "dst": True} and world.sub == {"src": True, "dst": False}
    assert all(world.configmap[ns][0] == INI.format(host="dst-db") for ns in ("src", "dst"))


def test_receipt_direction_is_cutover_when_leaving_origin(store, world):
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result
    receipt = execution.result["receipt"]
    assert (receipt["direction"], receipt["source"], receipt["target"]) == ("cutover", "env-aws", "env-onprem")


def after_cutover(w: World) -> None:
    """컷오버 DONE 뒤 상태: 온프렘만 쓰기, 역방향 구독만 켜짐, 두 PgBouncer는 온프렘을 가리킨다."""
    w.login, w.sub = {"src": False, "dst": True}, {"src": True, "dst": False}
    w.ini_host = {"src": "dst-db", "dst": "dst-db"}
    w.configmap = {ns: (INI.format(host="dst-db"), "1") for ns in ("src", "dst")}


def test_failback_returns_to_origin_with_failback_receipt(store, world):
    """T20: 출발지 · 목적지를 뒤바꾼 요청은 원래 출발지(AWS)로 돌아가는 되돌리기다."""
    after_cutover(world)
    execution_id = start(store, source="env-onprem", target="env-aws")
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result
    receipt = execution.result["receipt"]
    assert (receipt["direction"], receipt["source"], receipt["target"]) == ("failback", "env-onprem", "env-aws")
    # 되돌린 뒤: AWS만 쓰기, 정방향 구독만 켜짐, 두 PgBouncer는 AWS를 가리킨다
    assert world.login == {"src": True, "dst": False} and world.sub == {"src": False, "dst": True}
    assert world.ini_host == {"src": "src-db", "dst": "src-db"}


def test_config_requires_registered_origin():
    sites = dict(CONFIG.sites)
    with pytest.raises(ValidationError):
        Config(sites=sites)
    with pytest.raises(ValidationError, match="not a registered environment"):
        Config(sites=sites, origin="env-gcp")


def test_request_not_involving_origin_aborts_before_any_change(store, world):
    """원래 출발지를 떠나지도 돌아오지도 않는 요청은 방향을 정할 수 없다: 원격 조작 없이 쓰기 전 실패."""
    config = CONFIG.model_copy(update={"origin": "env-other", "sites": {**CONFIG.sites, "env-other": CONFIG.sites["env-aws"]}})
    cutover = FakeCutover(store, world)
    cutover._config = config
    execution_id = start(store)
    cutover.run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE
    assert "does not leave or return to origin" in execution.result["error"] and world.calls == []


def test_takeover_inside_commit_stops_further_changes_and_records(store, postgres_dsn, world):
    """6단계 첫 구독 변경 직후 넘겨받히면, 역방향 구독을 켜지 않고 committed_at도 쓰지 않는다."""
    from bravo_cutover.store import Store

    execution_id = start(store)
    newer = Store(postgres_dsn)
    world.after["dst.set_subscription:False"] = lambda: newer.claim(execution_id, takeover=True)
    try:
        FakeCutover(store, world).run(execution_id)
        execution = store.get(execution_id)
        assert world.calls[-1] == "dst.set_subscription:False"  # 그다음 원격 조작이 없다
        assert "src.set_subscription:True" not in world.calls
        assert execution.committed_at is None and execution.status == Status.RUNNING
        assert execution.steps[-1] == {"step": "commit", "status": "started"}  # 앞 프로세스는 더 기록하지 않았다
    finally:
        newer.close()


@pytest.mark.parametrize("point", ["src.entry:True", "src.pause", "src.set_login:False", "src.insert_barrier",
                                   "dst.set_login:True", "src.retarget:dst-db", "src.entry:False"])
def test_takeover_after_any_change_sends_no_more_changes(store, postgres_dsn, world, point):
    """어느 원격 변경 직후에 넘겨받혀도 앞 프로세스는 다음 변경을 보내지 않는다(병렬 해제 포함)."""
    from bravo_cutover.store import Store

    execution_id = start(store)
    newer = Store(postgres_dsn)
    before_takeover = []

    def takeover():
        before_takeover.append(store.get(execution_id).result)
        newer.claim(execution_id, takeover=True)

    world.after[point] = takeover
    try:
        FakeCutover(store, world).run(execution_id)
        after = world.calls[world.calls.index(point) + 1:]
        # 순서대로 하는 단계에서는 다음 변경이 하나도 없어야 한다. 경로마다 동시에 출발하는 진입점 차단 · 해제와
        # probe만, 넘겨받기 전에 이미 출발한 다른 경로의 호출이 남을 수 있다.
        in_flight = {"src.entry:True": {"dst.entry:True"},
                     "src.entry:False": {"dst.entry:False", "src.probe", "dst.probe"}}.get(point, set())
        assert set(after) <= in_flight, after
        assert store.get(execution_id).status == Status.RUNNING
        assert store.get(execution_id).result == before_takeover[0]
    finally:
        newer.close()


def test_takeover_during_source_restoration_retains_both_leases(store, postgres_dsn, world, monkeypatch):
    from bravo_cutover.store import LeaseHeld, Store

    dispatched, deliver = threading.Event(), threading.Event()
    set_login = FakeDb.set_login

    def delay_first_source_open(db, role, allowed):
        if db.ns == "src" and allowed and not dispatched.is_set():
            dispatched.set()
            assert deliver.wait(15)
        set_login(db, role, allowed)

    def fail_precommit(_run):
        raise RuntimeError("failure after source fencing")

    monkeypatch.setattr(FakeDb, "set_login", delay_first_source_open)
    first = start(store)
    replacement = Store(postgres_dsn)
    try:
        with ThreadPoolExecutor(1) as pool:
            old = FakeCutover(store, world, hooks={"before:compare": fail_precommit})
            running = pool.submit(old.run, first)
            try:
                assert dispatched.wait(10)
                assert FakeCutover(replacement, world).recover() == [first]
                assert replacement.get(first).status == Status.SAFE_HOLD
                assert world.login == {"src": False, "dst": False}
                with pytest.raises(LeaseHeld):
                    start(replacement)
            finally:
                deliver.set()
            running.result(timeout=10)
        assert world.login == {"src": True, "dst": False}
        assert replacement.get(first).status == Status.SAFE_HOLD
        with pytest.raises(LeaseHeld):
            start(replacement)
    finally:
        replacement.close()


@pytest.mark.parametrize("failure", ["error", "conflict"])
def test_configmap_not_persisted_is_safe_hold_not_done(store, world, failure):
    """ConfigMap 영구 반영 실패 · 충돌은 DONE이 아니다. SAFE_HOLD로 lease를 쥔 채 멈춘다."""
    if failure == "error":
        world.configmap_error["dst"] = Unconfirmed("patch configmap: forbidden")
        hooks = {}
    else:
        # 읽은 뒤 다른 쪽이 먼저 바꿨다: resourceVersion 충돌
        def concurrent_edit(_run):
            value, version = world.configmap["dst"]
            world.configmap["dst"] = (value + "# edited by someone\n", str(int(version) + 1))
        hooks = {"persist:read:env-onprem": concurrent_edit}
    execution_id = start(store)
    FakeCutover(store, world, hooks=hooks).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD, execution.result
    assert execution.result["failed_step"] == "persist"
    assert "edited by someone" in world.configmap["dst"][0] or failure == "error"  # 남의 수정을 덮어쓰지 않았다
    # lease를 쥐고 있어 같은 DB 쌍의 다음 실행은 거부된다
    from bravo_cutover.store import LeaseHeld

    with pytest.raises(LeaseHeld):
        start(store)


def test_pgbouncer_not_ready_fails_before_any_change(store, world):
    world.pod["dst"]["ready"] = False
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.result["failed_step"] == "precheck"
    assert not [c for c in world.calls if c.split(".")[1].startswith(("entry:True", "pause", "set_login"))]


@pytest.mark.parametrize(("when", "change"), [("src.entry:True", "uid"), ("src.pause", "restarts"),
                                              ("dst.set_login:True", "uid")])
def test_pgbouncer_pod_changed_during_execution_is_unknown(store, world, when, change):
    """사전 확인 때 고정한 PgBouncer 파드가 바뀌면(재생성 · 재시작) 상태 불명으로 본다."""
    def replace_pod():
        world.pod["src"][change] = "u-src-new" if change == "uid" else 1

    world.after[when] = replace_pod
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status in (Status.ABORT_PRE_WRITE, Status.SAFE_HOLD), execution.result
    assert "pgbouncer pod changed" in execution.result["error"]
    if execution.committed_at is None:
        assert execution.status == Status.SAFE_HOLD  # replacement process is not ours to resume
    else:
        assert execution.status == Status.SAFE_HOLD
    assert not (world.login["src"] and world.login["dst"])


def test_comparison_receipt_is_durable_before_target_opens_and_survives_hold(store, world):
    execution_id = start(store)

    def before_open(run):
        stored = store.get(execution_id)
        assert stored.result["receipt"]["barrier_id"] == run.receipt["barrier_id"]
        assert world.login["dst"] is False
        raise Unconfirmed("test interruption after commit, before target LOGIN")

    FakeCutover(store, world, hooks={"before:reconnect": before_open}).run(execution_id)
    stored = store.get(execution_id)
    assert stored.status == Status.SAFE_HOLD
    assert stored.result["receipt"]["tables"]
    assert stored.result["outcome"] == Status.SAFE_HOLD


def test_source_websockets_close_before_entry_release(store, world):
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    assert store.get(execution_id).status == Status.DONE
    assert world.calls.index("src.ws:1012") < world.calls.index("src.entry:False")
    assert "dst.ws:1012" not in world.calls
    assert store.get(execution_id).result["receipt"]["websocket_closed"] == {"app-0": 1}

@pytest.mark.parametrize("point", ["before:persist", "persist:read:env-aws"])
def test_pgbouncer_regenerated_around_persist_is_safe_hold(store, world, point):
    """#116 재검토: 마지막 ACK와 ConfigMap 변경 사이(또는 ConfigMap을 읽은 직후)에 PgBouncer가 재생성되면
    새 파드는 아직 출발지인 ConfigMap을 복사해 뜬다. ConfigMap만 목적지로 바꾸고 DONE으로 끝내지 않는다."""
    def regenerate(_run):
        world.pod["src"]["uid"] = "u-src-regenerated"
        world.ini_host["src"] = world.configmap["src"][0].split("host=")[1].split()[0]  # 옛 ConfigMap 복사
        world.paused["src"] = False

    execution_id = start(store)
    FakeCutover(store, world, hooks={point: regenerate}).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD, execution.result
    assert execution.result["failed_step"] == "persist"
    assert world.ini_host["src"] == "src-db"  # 실제 풀러는 여전히 출발지를 가리킨다
    from bravo_cutover.store import LeaseHeld

    with pytest.raises(LeaseHeld):
        start(store)  # lease를 쥐고 있다


def test_persist_requires_target_reachable_through_each_pool(store, world):
    """ConfigMap과 파드가 맞아도 풀러 경로로 목적지에 쓰지 못하면 DONE이 아니다."""
    def close_target(_run):
        world.login["dst"] = False

    execution_id = start(store)
    FakeCutover(store, world, hooks={"persist:read:env-onprem": close_target}).run(execution_id)
    assert store.get(execution_id).status == Status.SAFE_HOLD





@pytest.mark.parametrize("result", [
    {"verdict": "UNKNOWN", "hash_rule": "unavailable", "tables": TABLES},
    {"verdict": "UNVERIFIED", "hash_rule": "none", "tables": TABLES},  # 지운 임시 비교의 판정
    {"verdict": "match", "hash_rule": "v1", "tables": TABLES},  # 오타(대소문자)
    {"hash_rule": "v1", "tables": TABLES},  # verdict 누락
    match_with(tables=TABLES[:1]),  # 비교 대상 테이블 누락
    match_with(tables="all"),  # 잘못된 형식
    match_with(tables=[*TABLES, {**TABLES[0], "data_verdict": "MISMATCH"}]),  # 같은 표가 두 번, 하나는 MISMATCH
    match_with(drop=("data_verdict",)),  # 전체 데이터 판정 누락
    match_with(schema_verdict="UNKNOWN"),  # 전체는 MATCH인데 스키마 판정은 UNKNOWN
    match_with(drop=("hash_rule",)),  # 규칙 누락
    match_with(table=1, data_verdict="MISMATCH", match=False),  # 표별 판정은 MISMATCH, 전체만 MATCH
    match_with(table=0, drop=("data_verdict", "schema_verdict", "match", "source", "target")),  # 이름만 있는 표
    match_with(table=1, drop=("target",)),  # 지문 누락
    match_with(table=0, match="true"),  # 형식 오류
    "MATCH",  # dict가 아님
])
def test_only_explicit_match_opens_target(store, world, result):
    """#116 이상무 리뷰: MATCH만 진행한다. 그 밖에는 목적지를 열지 않고 목적지 시퀀스도 바꾸지 않는다."""
    world.seq["dst"] = SequenceState(1, False, 9, 2**63 - 1)  # MATCH였다면 올렸을 상태
    execution_id = start(store)
    FakeCutover(store, world, comparer=FixedComparer(result)).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert execution.result["failed_step"] == "compare"
    assert "dst.set_login:True" not in world.calls and world.login["dst"] is False
    assert "dst.raise_sequence" not in world.calls
    assert execution.result.get("receipt", {}).get("sequences", []) == []


def test_unverified_allowance_is_no_longer_a_setting():
    """임시 비교를 켜는 설정은 없다(spec D1). 남아 있는 설정 파일은 불러오기에서 거부된다."""
    with pytest.raises(ValidationError):
        Settings(allow_unverified_compare=True)


def test_match_raises_lagging_target_sequence_before_opening_target(store, world):
    """논리 복제는 시퀀스를 옮기지 않는다. MATCH 뒤 목적지 시퀀스를 출발지 다음 값까지 올리고, 그 뒤에 연다."""
    world.seq["src"] = SequenceState(41, True, 41, 2**63 - 1)
    world.seq["dst"] = SequenceState(1, False, 41, 2**63 - 1)
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result
    [sequence] = execution.result["receipt"]["sequences"]
    assert (sequence["src_next"], sequence["dst_set"], sequence["raised"]) == (42, 42, True)
    assert world.calls.index("dst.raise_sequence") < world.calls.index("dst.set_login:True")


def test_takeover_during_sequence_read_sends_no_raise(store, postgres_dsn, world):
    """#176 류지환 리뷰: 목적지 시퀀스를 읽는 도중 넘겨받히면, 앞 프로세스는 그 뒤 시퀀스를 올리지 않는다."""
    from bravo_cutover.store import Store

    world.seq["dst"] = SequenceState(1, False, 9, 2**63 - 1)  # 올려야 하는 상태
    execution_id = start(store)
    newer = Store(postgres_dsn)

    def takeover():
        newer.claim(execution_id, takeover=True)
        assert not store.owns(execution_id)

    world.on_sequence_read["dst"] = takeover
    try:
        FakeCutover(store, world).run(execution_id)
        assert "dst.raise_sequence" not in world.calls and world.calls[-1] == "src.insert_barrier"
        assert world.seq["dst"].next_value == 1
    finally:
        newer.close()


def test_unconfirmed_sequence_raise_aborts_before_write(store, world):
    """올린 값을 다시 읽어 확인하지 못하면 쓰기 전 실패다. 영수증에는 고정 코드만 남는다."""
    world.seq["dst"] = SequenceState(1, False, 1, 2**63 - 1)
    world.seq_readback_lost = True
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert execution.result["failed_step"] == "compare"
    assert execution.result["receipt"]["sequence_error"] == {"code": "sequence_readback_mismatch", "sqlstate": None}
    assert "dst.set_login:True" not in world.calls and world.login["dst"] is False


@pytest.mark.parametrize("leftover", ["session", "bypass"])
def test_target_session_opened_before_nologin_blocks_cutover(store, world, leftover):
    """NOLOGIN은 새 접속만 막는다. 목적지에 이미 붙은 앱 세션 · 우회 세션이 있으면 시작하지 않고,
    쓰기 전이라는 것을 증명할 수 없으니 출발지를 다시 열지도 않는다(SAFE_HOLD)."""
    if leftover == "session":
        world.sessions["dst"] = 1
    else:
        world.bypass["dst"] = ["batch_writer"]
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD, execution.result
    assert execution.result["failed_step"] == "precheck"
    assert world.calls == []  # 아무것도 바꾸지 않았다


def test_target_session_appearing_mid_run_keeps_source_closed(store, world):
    """쓰기 제한 뒤 목적지에 세션이 생긴 채 확정 전 실패가 나면, 원상 복구로 출발지를 다시 열지 않는다."""
    world.after["src.pause"] = lambda: world.sessions.__setitem__("dst", 1)
    execution_id = start(store)
    FakeCutover(store, world, comparer=FixedComparer({"verdict": "MISMATCH", "hash_rule": "v1",
                                                      "tables": TABLES})).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD, execution.result
    assert "src.set_login:True" not in world.calls and "src.resume" not in world.calls
    assert world.login["src"] is False  # 펜싱된 출발지를 열지 않았다


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        import time

        return time.monotonic() + self.offset


def test_budget_exceeded_before_commit_aborts(store, world):
    """#116 이상무 리뷰: 쓰기 정지 예산(5초)을 확정 전에 넘기면 쓰기 전 실패로 원상 복구한다."""
    clock = Clock()

    def slow(_run):
        clock.offset += 6

    execution_id = start(store)
    FakeCutover(store, world, clock=clock, hooks={"before:compare": slow}).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert "budget" in execution.result["error"] and execution.committed_at is None
    assert world.login == {"src": True, "dst": False}


def test_budget_exceeded_after_commit_finishes_and_reports(store, world):
    """확정 뒤에 예산을 넘기면 멈추지 않고(양쪽이 닫힌 채 남지 않게) 전환을 끝내되 예산 위반을 남긴다."""
    clock = Clock()

    def slow(_run):
        clock.offset += 6

    execution_id = start(store)
    FakeCutover(store, world, clock=clock, hooks={"before:reconnect": slow}).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result
    result = execution.result
    assert result["write_pause_budget_exceeded"] is True and result["write_pause_ms"] > 5000
    assert result["receipt"]["write_pause_budget_exceeded"] is True


def test_waits_are_capped_by_remaining_budget(store, world):
    """barrier 대기 같은 확정 전 대기는 남은 예산을 넘지 않는다."""
    clock = Clock()
    world.after["src.insert_barrier"] = lambda: (world.barriers.clear(), setattr(clock, "offset", 4.5))
    execution_id = start(store)
    import time

    began = time.monotonic()
    FakeCutover(store, world, clock=clock).run(execution_id)
    assert time.monotonic() - began < 3  # barrier_timeout_s(10초)까지 기다리지 않았다
    assert store.get(execution_id).status == Status.ABORT_PRE_WRITE


@pytest.mark.parametrize("restriction", ["nologin", "pause", "write_block"])
def test_preexisting_restrictions_are_not_released_on_precheck_failure(store, world, restriction):
    if restriction == "nologin":
        world.login["src"] = False
    elif restriction == "pause":
        world.paused["src"] = True
    else:
        world.blocked = {"src": True, "dst": True}
    before = (world.login.copy(), world.paused.copy(), world.blocked.copy())
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE
    assert execution.result["restored"] == []
    assert (world.login, world.paused, world.blocked) == before
    assert world.calls == []


class ProcessLost(BaseException):
    pass


@pytest.mark.parametrize("point", ["before:precheck", "before:entry", "before:pause", "before:compare"])
def test_restart_uses_durable_baseline_and_effect_boundary(store, postgres_dsn, world, point):
    from bravo_cutover.store import Store

    execution_id = start(store)

    def lost(_run):
        raise ProcessLost()

    with pytest.raises(ProcessLost):
        FakeCutover(store, world, hooks={point: lost}).run(execution_id)
    if point in ("before:precheck", "before:entry"):
        # Maintenance restriction after the process died must not be undone by recovery.
        world.login["src"] = False
        world.paused["src"] = True
        world.blocked = {"src": True, "dst": True}
    prior_calls = list(world.calls)
    newer = Store(postgres_dsn)
    try:
        FakeCutover(newer, world).recover()
        execution = newer.get(execution_id)
        assert execution.status == Status.ABORT_PRE_WRITE, execution.result
        if point in ("before:precheck", "before:entry"):
            assert world.calls == prior_calls
            assert world.login["src"] is False and world.paused["src"] is True
            assert all(world.blocked.values())
        elif point == "before:pause":
            assert "src.resume" not in world.calls and "src.set_login:True" not in world.calls
            assert not any(world.blocked.values())
        else:
            assert world.login == {"src": True, "dst": False}
    finally:
        newer.close()


def test_legacy_running_execution_without_baseline_is_held(store, world):
    execution_id = start(store)
    assert store.claim(execution_id)
    store.record_step(execution_id, "fence", "started")
    world.login["src"] = False
    FakeCutover(store, world).recover()
    assert store.get(execution_id).status == Status.SAFE_HOLD
    assert world.calls == [] and world.login["src"] is False


def test_block_added_between_precheck_and_entry_is_preserved(store, world):
    execution_id = start(store)

    def maintenance(_run):
        world.blocked["src"] = True

    FakeCutover(store, world, hooks={"before:entry": maintenance}).run(execution_id)
    assert store.get(execution_id).status == Status.ABORT_PRE_WRITE
    # 사전 확인이 쓴 것은 복제 경로 시험 행 하나뿐이다(ADR-0016). 차단은 그대로 둔다.
    assert world.blocked["src"] is True and world.calls == ["src.insert_path_probe"]


def test_process_loss_inside_precheck_preserves_existing_restrictions(store, world, monkeypatch):
    execution_id = start(store)
    world.blocked = {"src": True, "dst": True}

    def lost(_self):
        raise ProcessLost()

    with monkeypatch.context() as patch:
        patch.setattr(FakeEntry, "get", lost)
        with pytest.raises(ProcessLost):
            FakeCutover(store, world).run(execution_id)
    assert store.get(execution_id).result["recovery"] == {"version": 1, "baseline": None, "effects": []}
    FakeCutover(store, world).recover()
    assert store.get(execution_id).status == Status.ABORT_PRE_WRITE
    assert world.calls == [] and all(world.blocked.values())


def test_recovery_rereads_effects_committed_between_discovery_and_takeover(store, postgres_dsn, world, monkeypatch):
    from bravo_cutover.store import Store

    execution_id = start(store)
    captured = []

    def pause_before_entry(run):
        captured.append(run)
        raise ProcessLost()

    old = FakeCutover(store, world, hooks={"before:entry": pause_before_entry})
    with pytest.raises(ProcessLost):
        old.run(execution_id)
    stale = store.get(execution_id)
    assert stale.result["recovery"]["effects"] == []
    newer = Store(postgres_dsn)
    claim = newer.claim

    def racing_claim(identifier, *, takeover=False):
        old._entry(captured[0])  # old owner still owns it after discovery, before takeover
        return claim(identifier, takeover=takeover)

    monkeypatch.setattr(newer, "unfinished", lambda: [stale])
    monkeypatch.setattr(newer, "claim", racing_claim)
    try:
        FakeCutover(newer, world).recover()
        execution = newer.get(execution_id)
        assert execution.status == Status.ABORT_PRE_WRITE
        assert execution.result["recovery"]["effects"] == ["entry"]
        assert not any(world.blocked.values())
        assert "src.entry:False" in world.calls and "src.resume" not in world.calls
    finally:
        newer.close()


# 사전 확인의 복제 경로 왕복 시험 (T20, ADR-0016) -------------------------------------------

def fast_probe(cutover: Cutover, timeout_s: float = 0.2) -> Cutover:
    cutover._settings = cutover._settings.model_copy(update={"path_probe_timeout_s": timeout_s})
    return cutover


@pytest.mark.parametrize("direction", ["cutover", "failback"])
def test_path_down_aborts_before_any_write_restriction(store, world, direction):
    """경로가 끊겨 시험 행이 오지 않으면 진입점 · PAUSE · LOGIN · 구독을 하나도 바꾸지 않고 쓰기 전 실패다."""
    if direction == "failback":
        after_cutover(world)
        execution_id = start(store, source="env-onprem", target="env-aws")
        source_ns = "dst"
    else:
        execution_id = start(store)
        source_ns = "src"
    before = (dict(world.login), dict(world.sub), dict(world.paused), dict(world.blocked))
    world.path_down = True
    fast_probe(FakeCutover(store, world)).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert execution.result["failed_step"] == "precheck"
    assert execution.result["reason_code"] == "replication_path_unconfirmed"
    assert execution.result["remote_changes_started"] is False
    assert execution.result["path_probe"]["arrived"] is False
    assert world.calls == [f"{source_ns}.insert_path_probe"]
    assert (world.login, world.sub, world.paused, world.blocked) == before


def test_path_probe_runs_before_the_write_pause_and_is_recorded(store, world):
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result
    assert world.calls.index("src.insert_path_probe") < world.calls.index("src.entry:True")
    probe = execution.result["path_probe"]
    assert probe["arrived"] is True and probe["last_msg_receipt_time"] == "2026-11-04T00:00:00+00:00"
    # 시험 행은 5단계 barrier와 다른 행이다
    assert probe["probe_id"] != execution.result["receipt"]["barrier_id"]


def test_unreadable_receipt_time_does_not_decide(store, world, monkeypatch):
    """마지막 수신 시각은 진단용이다. 읽지 못해도 왕복 시험이 통과하면 진행한다."""
    def unreadable(self, subscription):
        raise Unconfirmed("pg_stat_subscription unreadable")

    monkeypatch.setattr(FakeDb, "last_msg_receipt_time", unreadable)
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result
    assert execution.result["path_probe"]["last_msg_receipt_time"] == "unknown"


def test_probe_seen_after_the_limit_is_not_accepted(store, world):
    """시험 행을 본 시각이 제한(기본 10초)을 넘었으면 늦은 도착이다. 성공으로 치지 않는다."""
    clock = Clock()
    world.after["src.insert_path_probe"] = lambda: setattr(clock, "offset", 20)
    execution_id = start(store)
    FakeCutover(store, world, clock=clock).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert execution.result["reason_code"] == "replication_path_unconfirmed"
    assert world.calls == ["src.insert_path_probe"]


def test_probe_limit_is_fixed_before_the_insert(store, world):
    """요청 deadline이 가까울 때 시험 행을 쓰는 데 걸린 시간을 남은 시간에서 두 번 빼지 않는다."""
    import time

    world.path_down = True
    world.after["src.insert_path_probe"] = lambda: time.sleep(1)
    execution_id = start(store, deadline=timedelta(seconds=1.8))
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    # 남은 약 1.7초를 다 기다린다(쓰기 1초를 빼고 0.7초만 기다리면 약 1초에서 실패한다)
    assert execution.result["path_probe"]["elapsed_ms"] >= 1400


def test_unreadable_target_during_probe_holds_without_changes(store, world, monkeypatch):
    """목적지 DB를 읽지 못하는 것은 경로 단절(확인된 실패)이 아니라 상태 불명이다: 바꾼 것 없이 SAFE_HOLD."""
    original = FakeDb.has_barrier

    def unreadable(self, barrier_id):
        if self.ns == "dst":
            raise Unconfirmed("target control connection lost")
        return original(self, barrier_id)

    monkeypatch.setattr(FakeDb, "has_barrier", unreadable)
    execution_id = start(store)
    FakeCutover(store, world).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD, execution.result
    assert execution.result["reason"] == "precheck could not confirm remote state; no changes sent"
    assert execution.result["path_probe"]["arrived"] is False
    assert world.calls == ["src.insert_path_probe"]
