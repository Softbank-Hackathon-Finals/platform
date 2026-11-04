"""차단 뒤 판정. 코드가 증거만 보고 정한다. 이 모듈은 LLM을 쓰지 않고 접속도 하지 않는다.

- FAIL: 잘못됐다는 증거가 있다. AWS 경로가 열렸거나, 복제가 끊겼거나, 앱이 뜨지 않았다.
- UNKNOWN: 증거가 빠졌거나 원인을 가를 수 없다. 대상이 내려가 있어도 거부처럼 보이기 때문에
  대조군이 닿지 않으면 거부로 세지 않는다.
- PASS: 필요한 증거가 모두 있고 전부 맞을 때만이다.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .cluster import SnapshotError, undeclared_egress
from .policy import POLICY_PREFIX, plan_is_intact

REPORT_VERSION = "exit-drill-report/v1"
OBSERVATION_VERSION = "exit-drill-observation/v1"
PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
EXIT_CODES = {PASS: 0, FAIL: 1, UNKNOWN: 3}

# 거부로 볼 수 있는 결과. CNI마다 막는 방식이 달라 timeout도 refused도 나온다(k3s는 REJECT).
DENIED_STATES = {"timeout", "refused", "unreachable"}
OPEN_STATES = {"connected", "resolved"}
# 차단 전후로 이 안에 잰 증거만 하나의 시험으로 묶는다.
MAX_WINDOW = timedelta(minutes=15)
# 정책이 Pod에 실제로 걸릴 때까지 기다린 것으로 치는 시간. k3s에서 기존 Pod는 0.2초, 새 Pod는
# 1초 안쪽이었다. 쿠버네티스 시각이 초 단위라 여유를 둔다.
ENFORCEMENT_MARGIN = timedelta(seconds=2)
_LSN = re.compile(r"^([0-9A-Fa-f]+)/([0-9A-Fa-f]+)$")


@dataclass(frozen=True)
class Check:
    id: str
    verdict: str
    code: str


def judge(plan: dict, before: Any, after: Any) -> dict:
    if not plan_is_intact(plan):
        return _report(plan, [Check("binding:plan", UNKNOWN, "PLAN_TAMPERED")])
    try:
        return _judge(plan, before, after)
    except (AttributeError, KeyError, TypeError, ValueError):
        # 관측 안의 값이 모양을 벗어났다. 예외로 죽으면 종료 코드 1이라 FAIL로 읽힌다.
        return _report(plan, [Check("binding:evidence", UNKNOWN, "OBSERVATION_UNREADABLE")])


def _judge(plan: dict, before: Any, after: Any) -> dict:
    binding = _check_binding(plan, before, after)
    if binding:
        # 다른 시험의 증거일 수 있으니 내용을 믿고 판정하지 않는다.
        return _report(plan, binding)
    applied = _time(after.get("policy_applied_at"))
    checks = [
        *_check_required(plan),
        *[Check(f"coverage:{d['id']}", UNKNOWN, "UNMAPPED_DEPENDENCY") for d in plan["unmapped_dependencies"]],
        _check_cutover(after),
        _check_pod_scope(after),
        _check_host_network(plan, after),
        _check_baseline_policy(plan, before),
        _check_applied_policy(plan, after),
    ]
    control = next((name for name, w in plan["workloads"].items() if w["role"] == "control"), None)
    for decision in plan["decisions"]:
        checks.extend(_check_decision(decision, control, before, after))
    app_workloads = sorted(name for name, w in plan["workloads"].items() if w["role"] == "app")
    policy_since = _policy_created(plan, after)
    checks.extend(_check_restart(name, plan["workloads"][name].get("container"), before, after, applied, policy_since) for name in app_workloads)
    newest = _newest_start(after, app_workloads)
    checks.extend(_check_new_connection(c["id"], after, newest) for c in plan["new_connection_checks"])
    return _report(plan, checks)


def exit_code(report: dict) -> int:
    return EXIT_CODES[report["verdict"]]


def _report(plan: dict, checks: list[Check]) -> dict:
    ordered = sorted(checks, key=lambda c: c.id)
    counts = {name: sum(1 for c in ordered if c.verdict == name) for name in (PASS, FAIL, UNKNOWN)}
    verdict = FAIL if counts[FAIL] else UNKNOWN if counts[UNKNOWN] or not ordered else PASS
    return {
        "schema_version": REPORT_VERSION,
        "verdict": verdict,
        "plan_digest": plan.get("digest"),
        "summary": counts,
        "checks": [{"id": c.id, "verdict": c.verdict, "code": c.code} for c in ordered],
    }


def _time(value: Any) -> datetime | None:
    """시간대가 있는 ISO 시각만 받는다. 시간대 없는 값은 전후 비교가 틀어질 수 있다."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _rows(observation: dict, key: str) -> list:
    rows = observation.get(key)
    return rows if isinstance(rows, list) else []


def _times(observation: dict) -> list[datetime | None]:
    rows = [*_rows(observation, "probes"), *_rows(observation, "replication"), *_rows(observation, "app_checks")]
    return [_time(row.get("observed_at")) if isinstance(row, dict) else None for row in rows]


def _check_binding(plan: dict, before: Any, after: Any) -> list[Check]:
    found = []
    for phase, observation in (("before", before), ("after", after)):
        if not isinstance(observation, dict):
            found.append(Check(f"binding:{phase}", UNKNOWN, "OBSERVATION_UNREADABLE"))
        elif observation.get("schema_version") != OBSERVATION_VERSION:
            found.append(Check(f"binding:{phase}", UNKNOWN, "OBSERVATION_VERSION"))
        elif observation.get("plan_digest") != plan["digest"]:
            found.append(Check(f"binding:{phase}", UNKNOWN, "PLAN_NOT_BOUND"))
        elif observation.get("phase") != phase:
            found.append(Check(f"binding:{phase}", UNKNOWN, "PHASE_MISMATCH"))
    if found:
        return found
    applied = _time(after.get("policy_applied_at"))
    before_times, after_times = _times(before), _times(after)
    if (
        applied is None
        or any(t is None or t >= applied for t in before_times)
        or any(t is None or t < applied for t in after_times)
    ):
        return [Check("binding:timing", UNKNOWN, "OBSERVED_OUTSIDE_PHASE")]
    if (after_times and max(after_times) - applied > MAX_WINDOW) or (before_times and applied - min(before_times) > MAX_WINDOW):
        return [Check("binding:timing", UNKNOWN, "OBSERVATION_WINDOW_TOO_LONG")]
    return []


def _check_required(plan: dict) -> list[Check]:
    """차단 · 유지 · 재시작 · 새 연결 중 하나라도 잴 것이 없으면 통과할 수 없다."""
    actions = {d["action"] for d in plan["decisions"]}
    present = {
        "block": "BLOCK" in actions,
        "keep": "KEEP" in actions,
        "restart": any(w["role"] == "app" for w in plan["workloads"].values()),
        "new-connection": bool(plan["new_connection_checks"]),
    }
    return [Check(f"required:{name}", UNKNOWN, "NOTHING_TO_JUDGE") for name, ok in present.items() if not ok]


def _check_cutover(after: dict) -> Check:
    """쓰기가 아직 출발지면 이 시험은 의미가 없다. 앱이 망가져도 앱 탓이 아니다."""
    cutover = after.get("cutover")
    if isinstance(cutover, dict) and cutover.get("writer") == "target":
        return Check("cutover:writer", PASS, "WRITER_IS_TARGET")
    return Check("cutover:writer", UNKNOWN, "CUTOVER_NOT_CONFIRMED")


def _check_pod_scope(after: dict) -> Check:
    """계획을 만든 뒤 새로 뜬 Pod는 어느 정책에도 안 걸리고 아무도 재지 않는다."""
    outside = after.get("unclassified_pods")
    if not isinstance(outside, list):
        return Check("scope:pods", UNKNOWN, "POD_SCOPE_UNKNOWN")
    if outside:
        return Check("scope:pods", UNKNOWN, "POD_OUTSIDE_PLAN")
    return Check("scope:pods", PASS, "ALL_PODS_IN_PLAN")


def _check_host_network(plan: dict, after: dict) -> Check:
    """hostNetwork Pod에는 NetworkPolicy가 걸리지 않는다. 계획을 만든 뒤 바뀔 수 있어 다시 본다."""
    pods = [p for name, w in plan["workloads"].items() if w["role"] == "app" for p in (_pods(after, name) or [])]
    if any(p.get("host_network") is True for p in pods):
        return Check("scope:host-network", FAIL, "HOST_NETWORK_POD")
    if any(p.get("host_network") is not False for p in pods):
        return Check("scope:host-network", UNKNOWN, "HOST_NETWORK_UNKNOWN")
    return Check("scope:host-network", PASS, "NO_HOST_NETWORK")


def _readback(observation: dict) -> tuple[list, list] | None:
    readback = observation.get("policy_readback")
    if not isinstance(readback, dict):
        return None
    start, end = readback.get("start"), readback.get("end")
    return (start, end) if isinstance(start, list) and isinstance(end, list) else None


def _specs(policies: list) -> dict:
    return {p["metadata"]["name"]: p["spec"] for p in policies}


def _check_baseline_policy(plan: dict, before: dict) -> Check:
    readback = _readback(before)
    if readback is None:
        return Check("policy:baseline", UNKNOWN, "POLICY_NOT_READ")
    ours = {m["metadata"]["name"] for m in plan["manifests"]}
    try:
        names = set(_specs(readback[0])) | set(_specs(readback[1]))
    except (KeyError, TypeError):
        return Check("policy:baseline", UNKNOWN, "POLICY_NOT_READ")
    if names & ours:
        return Check("policy:baseline", UNKNOWN, "BASELINE_NOT_CLEAN")
    return Check("policy:baseline", PASS, "BASELINE_CLEAN")


def _check_applied_policy(plan: dict, after: dict) -> Check:
    readback = _readback(after)
    if readback is None:
        return Check("policy:readback", UNKNOWN, "POLICY_NOT_READ")
    try:
        if any(p["metadata"]["namespace"] != plan["namespace"] for p in [*readback[0], *readback[1]]):
            return Check("policy:readback", UNKNOWN, "POLICY_NOT_READ")
        start, end = _specs(readback[0]), _specs(readback[1])
        if start != end:
            return Check("policy:readback", UNKNOWN, "POLICY_CHANGED_DURING_OBSERVATION")
        if _wider_than_plan(plan, after, readback[0]):
            return Check("policy:readback", FAIL, "POLICY_WIDER_THAN_PLAN")
    except (KeyError, TypeError, AttributeError, SnapshotError):
        return Check("policy:readback", UNKNOWN, "POLICY_NOT_READ")
    if any(start.get(m["metadata"]["name"]) != m["spec"] for m in plan["manifests"]):
        return Check("policy:readback", UNKNOWN, "POLICY_NOT_APPLIED")
    return Check("policy:readback", PASS, "POLICY_MATCHES_PLAN")


def _wider_than_plan(plan: dict, after: dict, policies: list) -> bool:
    ours = {m["metadata"]["name"]: m for m in plan["manifests"]}
    for name, workload in plan["workloads"].items():
        if workload["role"] != "app":
            continue
        allowed = ours[POLICY_PREFIX + name]["spec"]["egress"]
        for pod in _pods(after, name) or []:
            if undeclared_egress(policies, pod["labels"], allowed, skip_names=ours.keys()):
                return True
    return False


def _pods(observation: dict, workload: str) -> list | None:
    """읽지 못했으면 None이다. 알아볼 수 없는 항목이 하나라도 있으면 읽지 못한 것으로 친다."""
    pods = observation.get("pods")
    if not isinstance(pods, dict) or not isinstance(pods.get(workload), list):
        return None
    if not all(isinstance(p, dict) for p in pods[workload]):
        return None
    return pods[workload]


def _uids(observation: dict, workload: str | None) -> set:
    return {p.get("uid") for p in (_pods(observation, workload) or [])} if workload else set()


def _route_rows(observation: dict, route_id: str, vantage: str) -> list[dict]:
    return [
        row for row in _rows(observation, "probes")
        if isinstance(row, dict) and row.get("route_id") == route_id and row.get("vantage") == vantage
    ]


def _check_decision(decision: dict, control: str | None, before: dict, after: dict) -> list[Check]:
    route_id, action = decision["route_id"], decision["action"]
    if action == "KEEP" and decision["evidence"] == "stream":
        return [_check_stream(route_id, before, after)]
    checks = []
    for owner in decision["owners"]:
        if action == "BLOCK":
            checks.append(_check_block(route_id, owner, control, before, after))
        elif action == "ALLOW":
            checks.append(_check_open(f"local:{route_id}:{owner}", route_id, owner, before, after, "LOCAL_PATH_BROKEN"))
        else:
            checks.append(_check_open(f"keep:{route_id}:{owner}", route_id, owner, before, after, "REPLICATION_PATH_BROKEN"))
    return checks


def _was_open(route_id: str, owner: str, before: dict) -> bool:
    uids = _uids(before, owner)
    return any(r.get("state") in OPEN_STATES and r.get("pod_uid") in uids for r in _route_rows(before, route_id, owner))


def _check_block(route_id: str, owner: str, control: str | None, before: dict, after: dict) -> Check:
    check_id = f"block:{route_id}:{owner}"
    rows = _route_rows(after, route_id, owner)
    if not rows:
        return Check(check_id, UNKNOWN, "NOT_OBSERVED")
    uids = _uids(after, owner)
    if any(r.get("pod_uid") not in uids for r in rows):
        return Check(check_id, UNKNOWN, "VANTAGE_NOT_BOUND")
    states = {r.get("state") for r in rows}
    if "connected" in states:
        return Check(check_id, FAIL, "SOURCE_PATH_OPEN")
    if not states <= DENIED_STATES:
        return Check(check_id, UNKNOWN, "UNEXPECTED_STATE")
    if not _was_open(route_id, owner, before):
        return Check(check_id, UNKNOWN, "BASELINE_NOT_OPEN")
    if not _controls_surround(route_id, control, rows, after):
        return Check(check_id, UNKNOWN, "POSITIVE_CONTROL_FAILED")
    return Check(check_id, PASS, "DENIED")


def _controls_surround(route_id: str, control: str | None, target_rows: list[dict], after: dict) -> bool:
    """차단되지 않은 Pod가 재기 전과 후 모두 대상에 닿아야 거부를 정책 탓으로 돌릴 수 있다."""
    rows = _route_rows(after, route_id, control) if control else []
    uids = _uids(after, control)
    if not rows or any(r.get("state") != "connected" or r.get("pod_uid") not in uids for r in rows):
        return False
    control_times = [_time(r.get("observed_at")) for r in rows]
    target_times = [_time(r.get("observed_at")) for r in target_rows]
    if None in control_times or None in target_times:
        return False
    return min(control_times) <= min(target_times) and max(control_times) >= max(target_times)


def _check_open(check_id: str, route_id: str, owner: str, before: dict, after: dict, fail_code: str) -> Check:
    rows = _route_rows(after, route_id, owner)
    if not rows:
        return Check(check_id, UNKNOWN, "NOT_OBSERVED")
    uids = _uids(after, owner)
    if any(r.get("pod_uid") not in uids for r in rows):
        return Check(check_id, UNKNOWN, "VANTAGE_NOT_BOUND")
    states = {r.get("state") for r in rows}
    if states <= OPEN_STATES:
        return Check(check_id, PASS, "OPEN")
    if not states <= OPEN_STATES | DENIED_STATES | {"dns_failure"}:
        return Check(check_id, UNKNOWN, "UNEXPECTED_STATE")
    if not _was_open(route_id, owner, before):
        return Check(check_id, UNKNOWN, "BASELINE_NOT_OPEN")
    return Check(check_id, FAIL, fail_code)


def _lsn(value: Any) -> int | None:
    """PostgreSQL LSN(`상위/하위` 16진수)을 수로 바꾼다. 글자로 비교하면 0/9000 > 0/10000이 된다."""
    match = _LSN.match(value) if isinstance(value, str) else None
    return (int(match[1], 16) << 32) + int(match[2], 16) if match else None


def _stream_rows(observation: dict, route_id: str) -> list[dict]:
    rows = [r for r in _rows(observation, "replication") if isinstance(r, dict) and r.get("route_id") == route_id]
    return sorted(rows, key=lambda r: _time(r.get("observed_at")))


def _check_stream(route_id: str, before: dict, after: dict) -> Check:
    check_id = f"keep:{route_id}:stream"
    if not any(r.get("state") == "streaming" for r in _stream_rows(before, route_id)):
        return Check(check_id, UNKNOWN, "BASELINE_NOT_STREAMING")
    rows = _stream_rows(after, route_id)
    if any(isinstance(r.get("state"), str) and r["state"] != "streaming" for r in rows):
        return Check(check_id, FAIL, "REPLICATION_NOT_STREAMING")
    if len(rows) < 2 or any(r.get("state") != "streaming" for r in rows):
        return Check(check_id, UNKNOWN, "NOT_OBSERVED")
    first, last = _lsn(rows[0].get("lsn")), _lsn(rows[-1].get("lsn"))
    if first is None or last is None:
        return Check(check_id, UNKNOWN, "LSN_UNREADABLE")
    if last <= first:
        return Check(check_id, UNKNOWN, "REPLICATION_IDLE")
    return Check(check_id, PASS, "STREAMING_ADVANCED")


def _policy_created(plan: dict, after: dict) -> datetime | None:
    """클러스터가 기록한, 마지막 차단 정책의 생성 시각. 적용 시각은 사람이 적는 값이라 이것과 대조한다."""
    readback = _readback(after)
    if readback is None or not plan["manifests"]:
        return None
    created = {p["metadata"]["name"]: _time(p["metadata"].get("creationTimestamp")) for p in readback[0]}
    times = [created.get(m["metadata"]["name"]) for m in plan["manifests"]]
    return None if None in times else max(times)


def _check_restart(workload: str, container: Any, before: dict, after: dict, applied: datetime, policy_since: datetime | None) -> Check:
    check_id = f"restart:{workload}"
    now, was = _pods(after, workload), _pods(before, workload)
    if now is None or not was:
        return Check(check_id, UNKNOWN, "NOT_OBSERVED")
    if not now:
        return Check(check_id, FAIL, "NO_PODS")
    if any(p.get("ready") is False for p in now):
        return Check(check_id, FAIL, "NOT_READY")
    if any(p.get("ready") is not True for p in now):
        return Check(check_id, UNKNOWN, "READINESS_UNKNOWN")
    if {p.get("uid") for p in now} & {p.get("uid") for p in was}:
        return Check(check_id, UNKNOWN, "RESTART_NOT_OBSERVED")
    if not all(_container_known(p, container) for p in now):
        return Check(check_id, UNKNOWN, "CONTAINER_UNKNOWN")
    if policy_since is None:
        return Check(check_id, UNKNOWN, "POLICY_TIME_UNKNOWN")
    for pod in now:
        started = _time(pod.get("started_at"))
        if started is None or started < applied:
            return Check(check_id, UNKNOWN, "STARTED_BEFORE_BLOCK")
        running_since = _time(pod.get("container_started_at"))
        if running_since is None or running_since < policy_since + ENFORCEMENT_MARGIN:
            return Check(check_id, UNKNOWN, "STARTED_BEFORE_BLOCK")
    return _check_induced_restarts(check_id, now, after.get("induced_restarts"))


def _container_known(pod: dict, container: Any) -> bool:
    """계획이 정한 그 컨테이너의 상태를 빠짐없이 읽었는가. 앱 컨테이너가 아닌 값은 근거가 아니다."""
    return (
        isinstance(container, str)
        and pod.get("container_name") == container
        and isinstance(pod.get("container_id"), str)
        and bool(pod["container_id"])
        and type(pod.get("restarts")) is int
        and type(pod.get("other_restarts")) is int
    )


def _record_matches(record: Any, pod: dict) -> bool:
    """기록한 컨테이너가 실제로 끝났고 지금은 다른 컨테이너가 돈다."""
    if not isinstance(record, dict) or record.get("container") != pod["container_name"]:
        return False
    replaced = record.get("replaced_container_id")
    return isinstance(replaced, str) and replaced == pod.get("previous_container_id") and replaced != pod["container_id"]


def _check_induced_restarts(check_id: str, pods: list[dict], induced: Any) -> Check:
    """정책이 이미 걸린 Pod에서 앱 컨테이너만 다시 띄운 기록을 본다.

    Pod를 새로 만드는 것만으로는 "AWS 없이 뜬다"의 증거가 안 된다. 정책이 있어도 새 Pod는 처음
    잠깐 정책에 묶이지 않아, 시작할 때 AWS를 부르는 앱도 그 틈에 뜰 수 있다(k3s 실측, 매번 1초
    안쪽). 같은 Pod의 컨테이너 재시작은 그 틈이 없다. 단 Pod가 정책에 묶일 시간은 지나야 한다.
    새 Pod이므로 시킨 한 번만 재시작했어야 하고, 시키지 않은 재시작은 앱이 죽었다 살아난 것일 수 있다.
    같은 Pod의 다른 컨테이너가 다시 떴어도 앱의 근거가 되지 않는다. 차단 아래에서 죽은 것일 수
    있어 통과시키지도 않는다.
    """
    records = induced if isinstance(induced, dict) else {}
    for pod in pods:
        record, restarts = records.get(pod["uid"]), pod["restarts"]
        if restarts > (0 if record is None else 1):
            return Check(check_id, UNKNOWN, "UNSTABLE_RESTARTS")
        if record is None or restarts == 0:
            return Check(check_id, UNKNOWN, "STARTUP_NOT_ISOLATED")
        if not _record_matches(record, pod):
            return Check(check_id, UNKNOWN, "RESTART_RECORD_MISMATCH")
        if _time(pod.get("container_started_at")) < _time(pod.get("started_at")) + ENFORCEMENT_MARGIN:
            return Check(check_id, UNKNOWN, "STARTUP_NOT_ISOLATED")
        if pod["other_restarts"] != 0:
            return Check(check_id, UNKNOWN, "OTHER_CONTAINER_RESTARTED")
    return Check(check_id, PASS, "RESTARTED_READY")


def _newest_start(after: dict, workloads: list[str]) -> datetime | None:
    """앱 컨테이너가 마지막으로 시작한 시각. 새 연결은 이보다 뒤에 맺은 것이어야 한다."""
    pods = [p for name in workloads for p in (_pods(after, name) or [])]
    starts = [_time(p.get(field)) for p in pods for field in ("started_at", "container_started_at")]
    if not starts or None in starts:
        return None
    return max(starts)


def _check_new_connection(check: str, after: dict, newest: datetime | None) -> Check:
    check_id = f"new-connection:{check}"
    rows = [r for r in _rows(after, "app_checks") if isinstance(r, dict) and r.get("id") == check]
    if not rows:
        return Check(check_id, UNKNOWN, "NOT_OBSERVED")
    if any(r.get("ok") is False for r in rows):
        return Check(check_id, FAIL, "NEW_CONNECTION_FAILED")
    if any(r.get("ok") is not True for r in rows):
        return Check(check_id, UNKNOWN, "CHECK_UNREADABLE")
    times = [_time(r.get("observed_at")) for r in rows]
    if newest is None or None in times or min(times) < newest:
        return Check(check_id, UNKNOWN, "CHECK_BEFORE_RESTART")
    return Check(check_id, PASS, "NEW_CONNECTION_OK")
