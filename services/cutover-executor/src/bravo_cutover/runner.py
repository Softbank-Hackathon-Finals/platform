"""8단계 컷오버 실행기 (설계 문서 8.6, spec 2절).

안전은 DB의 앱 역할 로그인 상태로 지킨다. 진입점 차단과 PAUSE는 쓰기 정지 중 요청을 503 · 대기로 처리하는
수단이다. 쓰기 정지 시간은 첫 쓰기 제한 직전부터 모든 쓰기 경로(앱 파드 차단 해제, PgBouncer 경로)의
마지막 ACK까지다.
"""

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol
from uuid import UUID

from bravo_cutover.contract import CutoverRequest, Status, StepStatus
from bravo_cutover.db_fingerprint import (
    DbFingerprintComparer,
    FingerprintUnconfirmed,
    correct_sequence,
)
from bravo_cutover.remote import Unconfirmed
from bravo_cutover.remote.db import DbControl, WriteRefusal, app_write, try_app_write
from bravo_cutover.remote.kube import Kube, WriteBlock
from bravo_cutover.remote.pgbouncer import (
    PauseTimeout,
    PgBouncerAdmin,
    PgBouncerConfig,
    retarget_ini,
)
from bravo_cutover.sites import Config, Site
from bravo_cutover.store import Store

STEPS = ("precheck", "entry", "pause", "fence", "compare", "commit", "reconnect", "release", "persist")


class StepFailed(Exception):
    """조건을 확인했고, 그 결과가 실패다(상태 불명과 다르다). code는 결과의 reason_code로 남는다."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


REPLICATION_PATH_UNCONFIRMED = "replication_path_unconfirmed"


class OwnershipLost(Exception):
    """이 프로세스가 더 이상 실행을 맡고 있지 않다(잠금 연결이 끊겼거나 재시작 판정이 넘겨받았다).
    아무 원격 조작도 더 하지 않고, 실행 기록도 바꾸지 않고 멈춘다."""


class Comparer(Protocol):
    def compare(self, src: DbControl, dst: DbControl, tables: list[str]) -> dict[str, Any]: ...


@dataclass
class Side:
    site: Site
    kube: Kube
    db: DbControl = field(init=False)
    pool: PgBouncerAdmin = field(init=False)
    pool_config: PgBouncerConfig = field(init=False)
    entry: WriteBlock = field(init=False)

    def __post_init__(self):
        s = self.site
        self.db = DbControl(s.db_control_dsn, control_functions=s.control_functions)
        self.pool = PgBouncerAdmin(s.pgbouncer_admin_dsn, s.pgbouncer_database)
        self.pool_config = PgBouncerConfig(self.kube, s.namespace, s.pgbouncer_selector, s.pgbouncer_ini)
        self.entry = WriteBlock(self.kube, s.namespace, s.app_selector)


@dataclass
class Run:
    execution_id: UUID
    request: CutoverRequest
    src: Side
    dst: Side
    t0: float | None = None
    stages_ms: dict[str, int] = field(default_factory=dict)
    acks: dict[str, float] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    # 사전 확인 때 고정한 PgBouncer 파드 (이름, UID, 재시작 횟수). 실행 동안 바뀌면 상태 불명으로 본다.
    pools: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    step: str | None = None
    # 영수증의 방향(cutover | failback). 사전 확인에서 설정의 원래 출발지로 정한다.
    direction: str | None = None
    # 사전 확인의 복제 경로 왕복 시험 기록(ADR-0016). 복구 대상 효과가 아니다.
    path_probe: dict[str, Any] | None = None
    committed: bool = False
    recovery: dict[str, Any] = field(default_factory=lambda: {"version": 1, "baseline": None, "effects": []})


class _GuardedRaise:
    """correct_sequence에 넘기는 목적지. 읽기는 그대로 넘기고, 시퀀스를 올리는 호출만 _act로 보내
    실제 변경 직전에 소유를 확인한다(앞의 조회 도중 넘겨받혔으면 올리지 않는다)."""

    def __init__(self, db: DbControl, act: Callable[..., Any]):
        self._db, self._act = db, act

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    def raise_sequence(self, sequence: str, next_value: int) -> int:
        return self._act(self._db.raise_sequence, sequence, next_value)


def _not_matched(result: dict[str, Any], tables: list[str]) -> str | None:
    """진행해도 되는 비교 결과면 None, 아니면 이유. 허용 목록이다: 전체 · 표별 데이터 · 스키마 판정이 모두
    MATCH이고 설정한 표마다 지문이 하나씩 있을 때만 진행한다. 판정 값 · 칸 누락 · 형식 오류는 모두 쓰기 전 실패다.
    결과 모양은 T13 계약 문서 「결과 모양」."""
    verdict = result.get("verdict")
    if verdict == "MISMATCH":
        return "integrity comparison: MISMATCH"
    if verdict != "MATCH":
        return f"integrity comparison returned an unexpected verdict {verdict!r}"
    if not isinstance(result.get("hash_rule"), str) or not result["hash_rule"]:
        return "integrity comparison has no hash_rule"
    if result.get("data_verdict") != "MATCH" or result.get("schema_verdict") != "MATCH":
        return "integrity comparison: overall data or schema verdict is not MATCH"
    entries = result.get("tables")
    if not isinstance(entries, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in entries):
        return "integrity comparison: tables is not a list of named results"
    names = [t["name"] for t in entries]
    if missing := sorted(set(tables) - set(names)):
        return f"integrity comparison does not cover {missing}"
    if len(names) != len(set(names)):
        return "integrity comparison: duplicate table results"
    for t in entries:
        if t.get("match") is not True or t.get("data_verdict") != "MATCH" or t.get("schema_verdict") != "MATCH":
            return f"integrity comparison: table {t['name']} is not MATCH"
        if not isinstance(t.get("source"), dict) or not isinstance(t.get("target"), dict):
            return f"integrity comparison: table {t['name']} has no fingerprints"
    return None


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:500]


class Cutover:
    def __init__(self, store: Store, config: Config, *, comparer: Comparer | None = None,
                 kube: Callable[[str], Kube] = Kube, hooks: dict[str, Callable[["Run"], None]] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._store, self._config, self._settings = store, config, config.settings
        self._comparer = comparer or DbFingerprintComparer()
        self._kube_factory, self._kubes = kube, {}
        # 장애 주입 지점(시험 · T25용). 함수는 Run을 받는다: "before:<단계>", "commit:forward_disabled",
        # "commit:subscriptions_changed", "reconnect:target_opened", "release:<환경>", "persist:read:<환경>"
        self._hooks = hooks or {}
        self._clock = clock

    # 실행 ----------------------------------------------------------------
    def run(self, execution_id: UUID) -> None:
        if not self._store.claim(execution_id):
            return  # 이미 다른 프로세스가 맡았거나 끝났다
        execution = self._store.get(execution_id)
        request = CutoverRequest.model_validate(execution.request)
        try:
            run = Run(execution_id, request, self._side(request.source.environment_id),
                      self._side(request.target.environment_id))
        except Exception as e:  # noqa: BLE001
            # 원격 조작을 하나도 하지 않았다
            self._store.finish(execution_id, Status.ABORT_PRE_WRITE,
                               {"outcome": Status.ABORT_PRE_WRITE.value, "error": _describe(e), "restored": []})
            return
        try:
            self._save_recovery(run)
            for name in STEPS:
                run.step = name
                with self._step(run, name):
                    getattr(self, f"_{name}")(run)
        except OwnershipLost:
            return
        except Exception as e:  # noqa: BLE001
            self._fail(run, e)
            return
        self._store.finish(execution_id, Status.DONE, self._result(run))

    def _guard(self, run: "Run") -> None:
        """원격 상태를 바꾸기 전에 아직 이 프로세스가 실행을 맡고 있는지 확인한다.
        확인과 조작 사이의 짧은 틈, 이미 보낸 호출 하나의 결과는 불확실하게 남는다(ADR-0011)."""
        if not self._store.owns(run.execution_id):
            raise OwnershipLost(str(run.execution_id))

    def _act(self, run: "Run", fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """원격 상태를 바꾸는 호출은 모두 이것으로 한다. 부르기 직전마다 소유를 확인하므로,
        도중에 넘겨받히면 그다음 조작은 보내지 않는다."""
        self._guard(run)
        return fn(*args, **kwargs)

    def _record(self, run: "Run", step: str, status: str, elapsed_ms: int | None = None,
                detail: str | None = None) -> None:
        if not self._store.record_step(run.execution_id, step, status, elapsed_ms, detail):
            raise OwnershipLost(str(run.execution_id))

    def _mark(self, run: "Run", milestone: str) -> None:
        if not self._store.mark(run.execution_id, milestone):
            raise OwnershipLost(str(run.execution_id))

    def _same_pool(self, run: "Run", side: "Side") -> None:
        """PgBouncer 파드가 사전 확인 때와 같은지(재생성 · 컨테이너 재시작 · Ready 아님이면 다르다)."""
        env = side.site.environment_id
        current = side.pool_config.identity()
        if current != run.pools[env]:
            raise Unconfirmed(f"{env} pgbouncer pod changed during execution: {run.pools[env]} -> {current}")

    def _save_recovery(self, run: Run) -> None:
        if not self._store.record_recovery(run.execution_id, run.recovery):
            raise OwnershipLost(str(run.execution_id))

    def _fail(self, run: Run, error: Exception) -> None:
        """전환 확정 기록 전이면 원상 복구를 시도한다(ABORT_PRE_WRITE). 복구를 하나라도 확인하지 못하거나
        확정 뒤면 아무것도 새로 열지 않고 멈춘다(SAFE_HOLD, spec D6)."""
        failure = {"failed_step": run.step, "error": _describe(error)}
        if code := getattr(error, "code", None):
            failure["reason_code"] = code
        if run.path_probe is not None:
            failure["path_probe"] = run.path_probe
        if not self._store.owns(run.execution_id):
            return
        if self._store.get(run.execution_id).committed_at is not None:
            self._hold(run, failure, "failed after commit")
            return
        if run.recovery.get("version") != 1:
            self._hold(run, failure, "no durable recovery baseline from this executor version")
            return
        if not run.recovery["effects"]:
            # A precheck failure (including a crash) never authorizes unblocking anything. The precheck
            # writes only the path probe row (ADR-0016), which is not a recovery effect.
            if isinstance(error, Unconfirmed):
                self._hold(run, failure, "precheck could not confirm remote state; no changes sent")
            else:
                self._store.record_step(run.execution_id, "abort", StepStatus.SUCCEEDED)
                self._store.finish(run.execution_id, Status.ABORT_PRE_WRITE, {
                    "outcome": Status.ABORT_PRE_WRITE.value, **failure, "restored": [],
                    # 복구 대상 효과(진입점 · PAUSE · LOGIN · 구독)를 시작하지 않았다. 경로 시험 행은 path_probe에 남는다.
                    "remote_changes_started": False,
                })
            return
        restored: list[str] = []
        try:
            self._restore_source(run, restored)
        except OwnershipLost:
            return
        except Exception as e:  # noqa: BLE001
            self._store.record_step(run.execution_id, "abort", StepStatus.UNKNOWN, detail=_describe(e))
            self._hold(run, failure | {"abort_error": _describe(e), "restored": restored},
                       "abort could not be confirmed")
            return
        self._store.record_step(run.execution_id, "abort", StepStatus.SUCCEEDED)
        self._store.finish(run.execution_id, Status.ABORT_PRE_WRITE,
                           {"outcome": Status.ABORT_PRE_WRITE.value, **failure, "restored": restored})

    def _hold(self, run: Run, failure: dict[str, Any], reason: str) -> None:
        """SAFE_HOLD: 아무것도 새로 열거나 닫지 않는다(spec D10). 양쪽의 지금 상태를 읽어 남기고 사람을 기다린다."""
        self._store.finish(run.execution_id, Status.SAFE_HOLD, {
            "outcome": Status.SAFE_HOLD.value, **failure, "reason": reason, "observed": self._observe(run),
        })

    def _observe(self, run: Run) -> dict[str, Any]:
        def read(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return "unknown"

        sides = {}
        for role, side in (("source", run.src), ("target", run.dst)):
            s = side.site
            pool = read(side.pool.state)
            sides[role] = {
                "environment_id": s.environment_id,
                "app_role_can_login": read(lambda side=side, s=s: side.db.can_login(s.app_role)),
                "pgbouncer": pool if pool == "unknown" else {"paused": pool.paused, "host": pool.host},
                "write_block": read(side.entry.get),
                "subscription_enabled": read(lambda side=side, s=s: side.db.subscription(s.subscription).enabled),
            }
        return {
            "sides": sides,
            "writable": [r for r, v in sides.items() if v["app_role_can_login"] is True],
            "unknown": [r for r, v in sides.items() if v["app_role_can_login"] == "unknown"],
        }

    # 재시작 ---------------------------------------------------------------
    def recover(self) -> list[UUID]:
        """프로세스가 다시 떴을 때 끝나지 않은 실행을 판정한다. 하던 명령을 다시 보내지 않고,
        원격 상태를 읽어 ABORT_PRE_WRITE(확정 전 · 목적지 닫힘 확인) 또는 SAFE_HOLD로 끝낸다(spec D6)."""
        handled = []
        for execution in self._store.unfinished():
            was_pending = execution.status == Status.PENDING
            # 넘겨받는다. 앞 프로세스가 아직 살아 있어도 다음 조작 전에 소유를 잃은 것을 보고 멈춘다.
            if not self._store.claim(execution.execution_id, takeover=execution.status == Status.RUNNING):
                continue
            handled.append(execution.execution_id)
            # unfinished() is only a discovery snapshot. The old owner may have
            # persisted a mutation intent before this takeover; read it under our ownership.
            execution = self._store.get(execution.execution_id)
            if was_pending:
                # 실행기가 시작하기 전에 멈췄다. 원격 조작을 하나도 하지 않았다.
                self._store.finish(execution.execution_id, Status.ABORT_PRE_WRITE, {
                    "outcome": Status.ABORT_PRE_WRITE.value, "error": "executor restarted before the execution started",
                    "restored": [],
                })
                continue
            request = CutoverRequest.model_validate(execution.request)
            self._store.record_step(execution.execution_id, "recover", StepStatus.STARTED)
            started = [e["step"] for e in execution.steps if e["status"] == StepStatus.STARTED]
            try:
                run = Run(execution.execution_id, request, self._side(request.source.environment_id),
                          self._side(request.target.environment_id), step=started[-1] if started else None)
                run.recovery = (execution.result or {}).get("recovery", {})
                baseline = run.recovery.get("baseline") or {}
                run.pools = {env: tuple(identity) for env, identity in baseline.get("pools", {}).items()}
            except Exception as e:  # noqa: BLE001
                self._store.finish(execution.execution_id, Status.SAFE_HOLD, {
                    "outcome": Status.SAFE_HOLD.value, "reason": "cannot read remote state after restart",
                    "error": _describe(e),
                })
                continue
            # The previous owner records abort STARTED before sending source LOGIN.
            # Its SQL can still arrive after takeover. Retain both leases until it is reconciled.
            if "abort" in started:
                self._hold(run, {"failed_step": "abort"}, "source restoration interrupted; remote effects unconfirmed")
                continue
            self._fail(run, RuntimeError("executor restarted during execution"))
        return handled

    def _restore_source(self, run: Run, restored: list[str]) -> None:
        """컷오버 전 상태로 되돌리고 하나씩 확인한다. 이미 그 상태인 것은 건너뛴다(다시 불러도 같다)."""
        src, dst = run.src, run.dst
        baseline, effects = run.recovery["baseline"], run.recovery["effects"]
        if not baseline:
            raise Unconfirmed("remote mutation has no verified baseline")
        self._record(run, "abort", StepStatus.STARTED)
        # A replacement process, or a change outside our started stages, is not ours to undo.
        for side in (src, dst):
            self._same_pool(run, side)
            env = side.site.environment_id
            if side.entry.identities() != baseline["entries"][env]:
                raise Unconfirmed(f"{env} app pod set changed since precheck")
            state = side.pool.state()
            if state.host != src.site.db_host or (state.paused and "pause" not in effects):
                raise Unconfirmed(f"{env} pool changed outside this execution's effects")
        if "fence" not in effects and not src.db.can_login(src.site.app_role):
            raise Unconfirmed("source LOGIN changed outside this execution's effects")
        if "commit" not in effects and (src.db.subscription(src.site.subscription).enabled
                                       or not dst.db.subscription(dst.site.subscription).enabled):
            raise Unconfirmed("subscription changed outside this execution's effects")
        # 1) 목적지에 쓰지 않았고 쓸 수도 없음을 확인한다. 아니면 쓰기 전이라고 볼 수 없다.
        self._target_closed(run)
        restored.append("target_closed_confirmed")
        # 2) 구독: 역방향 끔 → 정방향 켬 (6단계 도중 실패했을 수 있다)
        if "commit" in effects and src.db.subscription(src.site.subscription).enabled:
            self._act(run, src.db.set_subscription, src.site.subscription, False, src.site.subscription_owner)
            restored.append("reverse_subscription_disabled")
        forward = dst.db.subscription(dst.site.subscription)
        if "commit" in effects and not (forward.enabled and forward.workers):
            self._act(run, dst.db.set_subscription, dst.site.subscription, True, dst.site.subscription_owner)
            restored.append("forward_subscription_enabled")
        # 3) 출발지 앱 역할 로그인. 출발지를 다시 열기 직전에 목적지가 여전히 닫혀 있는지 한 번 더 본다.
        self._target_closed(run)
        if "fence" in effects and not src.db.can_login(src.site.app_role):
            self._act(run, src.db.set_login, src.site.app_role, True)
            restored.append("source_login_restored")
        # 4) 두 PgBouncer: 출발지를 가리키는지 확인하고 RESUME. PAUSE 시간 초과로 걸려 있는 일시정지도
        #    여기서 푼다(목적지 닫힘을 확인한 뒤). 이미 풀려 있어도 RESUME은 오류 없이 끝난다.
        for side in (src, dst):
            state = side.pool.state()
            if "pause" in effects:
                self._act(run, side.pool.resume)
            if state.paused:
                restored.append(f"pgbouncer_resumed:{side.site.environment_id}")
            state = side.pool.state()
            if state.paused or state.host != src.site.db_host:
                raise Unconfirmed(f"{side.site.environment_id} pgbouncer not restored: {state}")
        # 5) 진입점 차단 해제
        for side in (src, dst):
            if any(self._act(run, side.entry.set, False).values()):
                raise Unconfirmed(f"{side.site.environment_id} write block still on")
        restored.append("entry_unblocked")
        # 6) 결과 시험: 출발지는 쓰기를 받고, 목적지는 거부한다
        deadline = self._clock() + self._settings.restore_verify_timeout_s
        while self._act(run, try_app_write, src.site.pgbouncer_app_dsn, f"abort:{run.execution_id}") != WriteRefusal.ACCEPTED:
            if self._clock() > deadline:
                raise Unconfirmed("source does not accept app writes after abort")
            time.sleep(0.5)
        if self._act(run, try_app_write, dst.site.db_app_dsn, f"abort:{run.execution_id}") != WriteRefusal.REFUSED:
            raise Unconfirmed("target does not refuse app writes after abort")
        restored.append("writes_verified")

    def _hook(self, point: str, run: "Run") -> None:
        if hook := self._hooks.get(point):
            hook(run)

    def _side(self, environment_id: str) -> Side:
        site = self._config.sites.get(environment_id)
        if site is None:
            raise StepFailed(f"unknown environment {environment_id}")
        if site.kube_context not in self._kubes:
            self._kubes[site.kube_context] = self._kube_factory(site.kube_context)
        return Side(site, self._kubes[site.kube_context])

    @contextmanager
    def _step(self, run: Run, name: str):
        if datetime.now(UTC) > run.request.deadline:
            raise StepFailed("deadline passed")
        self._hook(f"before:{name}", run)
        # 전환 확정 전에 쓰기 정지 예산을 다 썼으면 쓰기 전 실패로 멈춘다(원상 복구). 확정 뒤에는 멈추면 양쪽이
        # 모두 닫힌 채 남으므로 전환을 끝내고 결과에 예산 초과를 남긴다.
        if run.t0 is not None and not run.committed and self._budget_left_s(run) <= 0:
            raise StepFailed(f"write pause budget {run.request.write_pause_budget_ms}ms exceeded before commit")
        self._guard(run)
        self._record(run, name, StepStatus.STARTED)
        if name not in ("precheck", "entry"):
            if not run.recovery["baseline"]:
                raise Unconfirmed("remote step requires a durable verified baseline")
            run.recovery["effects"].append(name)
            self._save_recovery(run)  # a crash after this commit may have an in-flight effect
        start = self._clock()
        try:
            yield
        except OwnershipLost:
            raise
        except StepFailed as e:
            self._store.record_step(run.execution_id, name, StepStatus.FAILED, self._ms(start), str(e))
            raise
        except Exception as e:
            self._store.record_step(run.execution_id, name, StepStatus.UNKNOWN, self._ms(start), f"{type(e).__name__}: {e}")
            raise
        run.stages_ms[name] = self._ms(start)
        self._record(run, name, StepStatus.SUCCEEDED, run.stages_ms[name])

    def _ms(self, start: float) -> int:
        return int((self._clock() - start) * 1000)

    def _remaining_s(self, run: Run) -> float:
        return max(0.0, (run.request.deadline - datetime.now(UTC)).total_seconds())

    def _budget_left_s(self, run: Run) -> float:
        """쓰기 정지 예산(write_pause_budget_ms) 중 남은 시간. 첫 쓰기 제한(t0) 전에는 예산 전체다."""
        budget = run.request.write_pause_budget_ms / 1000
        return budget if run.t0 is None else budget - (self._clock() - run.t0)

    def _wait_limit_s(self, run: Run, configured_s: float) -> float:
        """전환 확정 전 대기의 시간 제한: 설정값 · 남은 deadline · 남은 쓰기 정지 예산 중 가장 짧은 것."""
        return max(0.0, min(configured_s, self._remaining_s(run), self._budget_left_s(run)))

    def _target_closed(self, run: Run) -> None:
        """목적지가 앱 쓰기를 받을 수 없음을 확인한다. NOLOGIN은 새 접속만 막으므로(기존 연결은 계속 쓸 수 있다)
        이미 붙은 앱 역할 세션과, 업무 역할이 아닌 클라이언트 세션(우회 쓰기 후보)도 0이어야 한다.
        확인되지 않으면 Unconfirmed: 확정 전이라도 원상 복구로 출발지를 열지 않고 SAFE_HOLD가 된다."""
        dst = run.dst
        role = dst.site.app_role
        if dst.db.can_login(role):
            raise Unconfirmed("target app role can log in: cannot prove no write happened on target")
        if sessions := dst.db.sessions(role):
            raise Unconfirmed(f"target still has {sessions} app session(s) opened before NOLOGIN")
        if bypass := dst.db.other_sessions(dst.site.system_roles + [role]):
            raise Unconfirmed(f"possible bypass writers on target: {bypass}")

    # 1. 사전 확인 -----------------------------------------------------------
    def _precheck(self, run: Run) -> None:
        src, dst = run.src, run.dst
        try:
            run.direction = self._config.direction(src.site.environment_id, dst.site.environment_id)
        except ValueError as e:
            raise StepFailed(str(e)) from e
        entries = {}
        for side in (src, dst):
            # PgBouncer 파드가 정확히 하나이고 Ready. 이 파드를 실행 끝까지 고정한다.
            run.pools[side.site.environment_id] = side.pool_config.identity()
            state = side.pool.state()
            if state.paused or state.host != src.site.db_host:
                raise StepFailed(f"{side.site.environment_id} pgbouncer is not serving the source: {state}")
            identity = side.entry.identities()
            blocks = side.entry.get()
            if set(blocks) != {p["name"] for p in identity} or side.entry.identities() != identity:
                raise Unconfirmed("app pod set changed while checking write blocks")
            if any(blocks.values()):
                raise StepFailed(f"{side.site.environment_id} already has an entry write block")
            entries[side.site.environment_id] = identity
        self._target_closed(run)
        if not src.db.can_login(src.site.app_role):
            raise StepFailed("source app role cannot log in")
        forward = dst.db.subscription(dst.site.subscription)
        if not (forward.enabled and forward.workers):
            raise StepFailed(f"forward subscription is not running: {forward}")
        if src.db.subscription(src.site.subscription).enabled:
            raise StepFailed("reverse subscription is already enabled")
        received = dst.db.received_lsn(dst.site.subscription)
        lag = int(src.db.current_wal_lsn()) - int(received or 0)
        if received is None or lag > self._settings.max_lag_bytes:
            raise StepFailed(f"replication lag {lag} bytes exceeds {self._settings.max_lag_bytes}")
        self._probe_path(run)
        run.recovery["baseline"] = {"pools": run.pools, "entries": entries,
                                    "source_login": True, "target_login": False,
                                    "forward_enabled": True, "reverse_enabled": False,
                                    "pools_paused": False, "entries_blocked": False,
                                    "pool_host": src.site.db_host}
        self._save_recovery(run)
        # 첫 쓰기 제한 직전에 타이머를 켠다
        run.t0 = self._clock()

    def _probe_path(self, run: Run) -> None:
        """쓰기를 막기 전에 출발지 → 목적지 복제 경로가 실제로 데이터를 옮기는지 본다(ADR-0016).
        시험 행이 시간 안에 목적지에 보이지 않으면 확인된 실패로 끝낸다(아무것도 막지 않았다).
        마지막 수신 시각은 진단용으로만 남긴다."""
        src, dst = run.src, run.dst
        probe = uuid.uuid4()
        try:
            receipt_time = dst.db.last_msg_receipt_time(dst.site.subscription)
        except Exception:  # noqa: BLE001
            receipt_time = "unknown"
        run.path_probe = {"probe_id": str(probe), "arrived": False, "elapsed_ms": None,
                          "last_msg_receipt_time": receipt_time}
        # 제한은 시험 행을 쓰기 전에 정한다(쓰는 데 걸린 시간을 남은 deadline에서 두 번 빼지 않는다).
        limit = min(self._settings.path_probe_timeout_s, self._remaining_s(run))
        start = self._clock()
        self._act(run, src.db.insert_path_probe, probe, run.request.epoch)
        # 목적지 DB를 읽지 못하면(Unconfirmed) 상태 불명이다: 사전 확인 규칙대로 SAFE_HOLD(바꾼 것 없음).
        while True:
            arrived = dst.db.has_barrier(probe)
            # 도착을 본 시각이 제한을 넘었으면 늦은 도착이다. 성공으로 치지 않는다.
            if self._clock() - start > limit:
                run.path_probe["elapsed_ms"] = self._ms(start)
                raise StepFailed(f"replication path probe did not arrive at {dst.site.environment_id} "
                                 f"within {limit:.3g}s", code=REPLICATION_PATH_UNCONFIRMED)
            if arrived:
                break
            time.sleep(0.05)
        run.path_probe.update(arrived=True, elapsed_ms=self._ms(start))

    # 2. 진입점 쓰기 차단 -------------------------------------------------------
    def _entry(self, run: Run) -> None:
        sides = (run.src, run.dst)
        for side in sides:
            env = side.site.environment_id
            if side.entry.identities() != run.recovery["baseline"]["entries"][env]:
                raise Unconfirmed(f"{env} app pod set changed before entry")
            if any(side.entry.get().values()):
                raise StepFailed(f"{env} acquired an entry block after precheck")
        run.recovery["effects"].append("entry")
        self._save_recovery(run)
        with ThreadPoolExecutor(max_workers=2) as pool:
            applied = list(pool.map(lambda side: self._act(run, side.entry.set, True), sides))
        for side, states in zip(sides, applied, strict=True):
            if not all(states.values()):
                raise Unconfirmed(f"{side.site.environment_id} write block not applied: {states}")
            # 차단하는 동안 새로 뜬 파드가 있으면 그 파드에도 건다. 아직 뜨는 중인 파드는 DB 쪽(PAUSE · NOLOGIN)이 막는다.
            if set(side.entry.running()) - set(states):
                states = self._act(run, side.entry.set, True)
                if not all(states.values()):
                    raise Unconfirmed(f"{side.site.environment_id} write block missing on {states}")

    # 3. PAUSE --------------------------------------------------------------
    def _pause(self, run: Run) -> None:
        src = run.src
        if src.db.long_transactions(src.site.app_role, self._settings.long_transaction_ms):
            raise StepFailed("long transaction open on source")
        for side in (run.src, run.dst):
            timeout = self._wait_limit_s(run, self._settings.pause_timeout_s)
            self._same_pool(run, side)
            try:
                self._act(run, side.pool.pause, timeout)
            except PauseTimeout as e:
                raise StepFailed(str(e)) from e
            self._same_pool(run, side)  # PAUSE를 받은 프로세스가 그대로인지
            if not side.pool.state().paused:
                raise Unconfirmed(f"{side.site.environment_id} pgbouncer not paused")

    # 4. 출발지 펜싱 -----------------------------------------------------------
    def _fence(self, run: Run) -> None:
        src = run.src
        role = src.site.app_role
        self._act(run, src.db.set_login, role, False)
        deadline = self._clock() + self._wait_limit_s(run, self._settings.fence_timeout_s)
        while self._act(run, src.db.terminate_sessions, role) or src.db.sessions(role):
            if self._clock() > deadline:
                raise StepFailed("app sessions still open on source")
            time.sleep(0.05)
        if bypass := src.db.other_sessions(src.site.system_roles + [role]):
            raise StepFailed(f"possible bypass writers on source: {bypass}")
        refusal = self._act(run, try_app_write, src.site.db_app_dsn, f"fence-check:{run.execution_id}")
        if refusal == WriteRefusal.ACCEPTED:
            raise StepFailed("source accepted an app write after fencing")
        if refusal == WriteRefusal.UNKNOWN:
            raise Unconfirmed("could not confirm that source refuses app writes")

    # 5. barrier · 비교 · 시퀀스 -------------------------------------------------
    def _compare(self, run: Run) -> None:
        src, dst = run.src, run.dst
        barrier = uuid.uuid4()
        self._act(run, src.db.insert_barrier, barrier, run.request.epoch)
        source_lsn = int(src.db.current_wal_lsn())
        deadline = self._clock() + self._wait_limit_s(run, self._settings.barrier_timeout_s)
        while not dst.db.has_barrier(barrier):
            if self._clock() > deadline:
                raise StepFailed("barrier did not arrive at target")
            time.sleep(0.05)
        result = self._comparer.compare(src.db, dst.db, self._settings.tables)
        if not isinstance(result, dict):
            raise StepFailed(f"integrity comparison returned {type(result).__name__}, not a result")
        tables = result.get("tables") if isinstance(result.get("tables"), list) else []
        verdict = result.get("verdict")
        problem = _not_matched(result, self._settings.tables)
        run.receipt = {
            "receipt_id": str(uuid.uuid4()),
            "epoch": run.request.epoch,
            "direction": run.direction,
            "source": src.site.environment_id,
            "target": dst.site.environment_id,
            "barrier_id": str(barrier),
            "watermark": {"source_lsn": source_lsn,
                          "received_lsn": int(dst.db.received_lsn(dst.site.subscription) or 0)},
            "hash_rule": result.get("hash_rule"),
            "tables": tables,
            "sequences": [],
            "verdict": verdict,
            "data_verdict": result.get("data_verdict"),
            "schema_verdict": result.get("schema_verdict"),
            "write_pause_budget_ms": run.request.write_pause_budget_ms,
        }
        # 시퀀스는 비교가 MATCH일 때만 올린다(spec D2). 중단될 실행에서는 목적지에 아무것도 쓰지 않는다.
        sequence_error = None
        if problem is None:
            try:
                for name, (table, column) in self._settings.sequences.items():
                    run.receipt["sequences"].append(
                        correct_sequence(src.db, _GuardedRaise(dst.db, partial(self._act, run)), name, table, column))
            except FingerprintUnconfirmed as e:
                sequence_error = e
                run.receipt["sequence_error"] = {"code": e.code, "sqlstate": e.sqlstate}
        self._guard(run)
        if not self._store.record_receipt(run.execution_id, run.receipt):
            raise OwnershipLost(str(run.execution_id))
        if problem is not None:
            raise StepFailed(problem)
        if sequence_error is not None:
            raise sequence_error

    # 6. 전환 확정 ------------------------------------------------------------
    def _commit(self, run: Run) -> None:
        src, dst = run.src, run.dst
        # 구독을 바꾸기 전에 남긴다. 재시작하면 이 기록으로 구독 복원이 필요한지 안다.
        self._mark(run, "stage6_entered_at")
        self._act(run, dst.db.set_subscription, dst.site.subscription, False, dst.site.subscription_owner)
        self._hook("commit:forward_disabled", run)
        self._act(run, src.db.set_subscription, src.site.subscription, True, src.site.subscription_owner)
        self._hook("commit:subscriptions_changed", run)
        self._mark(run, "committed_at")
        run.committed = True
        run.receipt["committed_at"] = datetime.now(UTC).isoformat()

    # 7. 재연결 ---------------------------------------------------------------
    def _reconnect(self, run: Run) -> None:
        dst = run.dst
        self._act(run, dst.db.set_login, dst.site.app_role, True)
        self._hook("reconnect:target_opened", run)
        for side in (run.src, run.dst):
            pinned = run.pools[side.site.environment_id]
            self._same_pool(run, side)
            self._act(run, side.pool_config.retarget, side.site.pgbouncer_database, dst.site.db_host,
                      dst.site.db_port, dst.site.db_name, pod=pinned[0])
            self._act(run, side.pool.reload)
            if side.pool.state().host != dst.site.db_host:
                raise Unconfirmed(f"{side.site.environment_id} pgbouncer did not switch to target")
            self._act(run, side.pool.resume)
            if side.pool.state().paused:
                raise Unconfirmed(f"{side.site.environment_id} pgbouncer still paused")
            self._same_pool(run, side)  # 설정을 바꾼 파드와 RELOAD · RESUME을 받은 프로세스가 같은지
        run.receipt["websocket_closed"] = self._act(run, run.src.entry.close_websockets)

    # 8. 해제와 마지막 ACK ---------------------------------------------------------
    def _release(self, run: Run) -> None:
        def release(side: Side) -> None:
            env = side.site.environment_id
            self._hook(f"release:{env}", run)
            if any(self._act(run, side.entry.set, False).values()):
                raise Unconfirmed(f"{env} write block still on")
            run.acks[f"entry:{env}"] = self._clock()

        def probe(side: Side) -> None:
            self._act(run, app_write, side.site.pgbouncer_app_dsn, f"cutover:{run.execution_id}")
            run.acks[f"pool:{side.site.environment_id}"] = self._clock()

        # 경로마다 동시에 열고, 각 경로의 ACK 시각을 따로 남긴다. 쓰기 정지는 그중 마지막 ACK에서 끝난다.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(f, side) for side in (run.src, run.dst) for f in (release, probe)]
        for future in futures:
            future.result()

    # 9. 설정 영구 반영 ---------------------------------------------------------
    def _persist(self, run: Run) -> None:
        """PgBouncer가 재시작해도 목적지를 가리키게 ConfigMap을 바꾼다(쓰기 정지 시간 밖).
        읽은 resourceVersion을 조건으로 바꿔 그 사이 다른 수정은 덮어쓰지 않는다. 확인하지 못하면 실패로
        올려 SAFE_HOLD가 되고 lease는 유지된다(재생성된 PgBouncer가 출발지 설정으로 뜨는 상태를 DONE으로 두지 않는다)."""
        dst = run.dst.site
        for side in (run.src, run.dst):
            s = side.site
            self._same_pool(run, side)
            current, version = side.kube.read_configmap_versioned(
                s.namespace, s.pgbouncer_configmap, s.pgbouncer_configmap_key)
            self._hook(f"persist:read:{s.environment_id}", run)
            try:
                wanted = retarget_ini(current, s.pgbouncer_database, dst.db_host, dst.db_port, dst.db_name)
            except ValueError as e:
                raise Unconfirmed(f"{s.environment_id} pgbouncer configmap: {e}") from e
            if current != wanted:
                self._act(run, side.kube.patch_configmap, s.namespace, s.pgbouncer_configmap,
                          s.pgbouncer_configmap_key, wanted, version)
            if side.kube.read_configmap(s.namespace, s.pgbouncer_configmap, s.pgbouncer_configmap_key) != wanted:
                raise Unconfirmed(f"{s.environment_id} pgbouncer configmap does not point to target")
        # ConfigMap만 보면, 마지막 ACK와 ConfigMap 변경 사이에 재생성된 PgBouncer(옛 ConfigMap = 출발지를 복사해
        # 뜬 파드)를 놓친다(#116 재검토). 실제로 도는 풀러까지 완료 조건으로 본다: 고정한 파드가 그대로이고,
        # 목적지를 가리키며 PAUSE가 풀려 있고, 그 경로로 쓰기가 된다. 하나라도 아니면 SAFE_HOLD(lease 유지).
        for side in (run.src, run.dst):
            env = side.site.environment_id
            self._same_pool(run, side)
            state = side.pool.state()
            if state.host != dst.db_host or state.paused:
                raise Unconfirmed(f"{env} pgbouncer is not serving the target after persist: {state}")
            self._act(run, app_write, side.site.pgbouncer_app_dsn, f"persist:{run.execution_id}")

    def _result(self, run: Run) -> dict[str, Any]:
        last = max(run.acks.values())
        write_pause_ms = int((last - run.t0) * 1000)
        budget_exceeded = write_pause_ms > run.request.write_pause_budget_ms
        run.receipt["write_pause_ms"] = write_pause_ms
        run.receipt["write_pause_budget_exceeded"] = budget_exceeded
        return {
            "outcome": Status.DONE.value,
            "write_pause_ms": write_pause_ms,
            "write_pause_budget_ms": run.request.write_pause_budget_ms,
            # 확정 뒤에 예산을 넘긴 경우: 전환은 끝냈지만(쓰기 DB는 목적지 하나) 예산 위반이다
            "write_pause_budget_exceeded": budget_exceeded,
            "stages_ms": run.stages_ms,
            "path_probe": run.path_probe,
            "path_acks_ms": {path: int((t - run.t0) * 1000) for path, t in run.acks.items()},
            "receipt": run.receipt,
        }


class ThreadRunner:
    """API가 요청을 받으면 기록을 남긴 뒤 이 실행기로 넘긴다. 한 번에 한 실행만 돈다.

    시작할 때 다른 Executor 프로세스가 없는지 확인하고(DB 잠금), 끝나지 않은 실행부터 판정한다.
    """

    def __init__(self, cutover: Cutover, store: Store | None = None):
        if store is not None and not store.acquire_singleton():
            raise RuntimeError("another cutover executor holds the executor lock")
        self._cutover = cutover
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cutover")
        if store is not None:
            cutover.recover()

    def submit(self, execution_id: UUID) -> None:
        self._pool.submit(self._cutover.run, execution_id)
