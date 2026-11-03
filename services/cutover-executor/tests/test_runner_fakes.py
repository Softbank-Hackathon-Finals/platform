"""원격 대상을 가짜로 둔 실행기 시험 (CI에서도 돈다. 실행 기록은 docker Postgres).

#116 리뷰(류지환) 3건의 회귀 시험:
- 단계 안에서 소유를 잃으면 그다음 원격 조작과 실행 기록을 하지 않는다
- ConfigMap 영구 반영 실패 · 충돌은 DONE이 아니라 SAFE_HOLD(lease 유지)
- PgBouncer 파드가 Ready가 아니거나 실행 도중 바뀌면 상태 불명으로 본다
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from bravo_cutover import runner as runner_module
from bravo_cutover.contract import CutoverRequest, Status
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
    settings=Settings(allow_unverified_compare=True, restore_verify_timeout_s=1),
    sites={"env-aws": site("env-aws", "src", "bravo_rev"), "env-onprem": site("env-onprem", "dst", "bravo_fwd")},
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
        return 0

    def terminate_sessions(self, role):
        self.w.did(f"{self.ns}.terminate_sessions")
        return 0

    def long_transactions(self, role, older_than_ms):
        return 0

    def other_sessions(self, roles):
        return []

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

    def has_barrier(self, barrier_id):
        return barrier_id in self.w.barriers

    def row_count(self, table):
        return 5

    def sequence_next(self, sequence):
        return 10

    def max_value(self, table, column):
        return 9

    def raise_sequence(self, sequence, next_value):
        self.w.did(f"{self.ns}.raise_sequence")
        return max(10, next_value)


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


class FakeCutover(Cutover):
    def __init__(self, store, world: World, **kwargs):
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


def start(store) -> uuid.UUID:
    request = CutoverRequest(
        plan_id="plan-fake", epoch=int(datetime.now(UTC).timestamp() * 1e6),
        source={"environment_id": "env-aws", "connection_ref": "fake/src"},
        target={"environment_id": "env-onprem", "connection_ref": "fake/dst"},
        write_pause_budget_ms=5000, deadline=datetime.now(UTC) + timedelta(minutes=1),
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
    world.after[point] = lambda: newer.claim(execution_id, takeover=True)
    try:
        FakeCutover(store, world).run(execution_id)
        after = world.calls[world.calls.index(point) + 1:]
        # 순서대로 하는 단계에서는 다음 변경이 하나도 없어야 한다. 경로마다 동시에 출발하는 진입점 차단 · 해제와
        # probe만, 넘겨받기 전에 이미 출발한 다른 경로의 호출이 남을 수 있다.
        in_flight = {"src.entry:True": {"dst.entry:True"},
                     "src.entry:False": {"dst.entry:False", "src.probe", "dst.probe"}}.get(point, set())
        assert set(after) <= in_flight, after
        assert store.get(execution_id).status == Status.RUNNING
        assert store.get(execution_id).result is None
    finally:
        newer.close()


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
        assert execution.status == Status.ABORT_PRE_WRITE  # 확정 전이면 원상 복구
    else:
        assert execution.status == Status.SAFE_HOLD
    assert not (world.login["src"] and world.login["dst"])


