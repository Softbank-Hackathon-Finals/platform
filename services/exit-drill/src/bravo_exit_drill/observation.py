"""읽은 결과를 판정기 입력(관측 파일)으로 묶는다. 값을 해석하거나 고치지 않는다.

모르는 값은 모른다고 둔다. Ready 조건이 없으면 False가 아니라 None이고, 판정기가 UNKNOWN으로 본다.
"""

from typing import Any

from .cluster import selector_matches
from .verdict import OBSERVATION_VERSION

PHASES = {"before", "after"}
_FINISHED = {"Succeeded", "Failed"}


class ObservationError(ValueError):
    """입력이 이 시험의 것이 아니다."""


def normalize_pods(doc: dict, plan: dict) -> dict[str, list[dict]]:
    """`kubectl get pods -o json`을 워크로드별로 나눈다. 끝났거나 종료 중인 Pod는 세지 않는다."""
    return _classify_pods(doc, plan)[0]


def _classify_pods(doc: dict, plan: dict) -> tuple[dict[str, list[dict]], list[str]]:
    grouped: dict[str, list[dict]] = {name: [] for name in plan["workloads"]}
    outside: list[str] = []
    for item in doc.get("items") or []:
        metadata = item.get("metadata") or {}
        if metadata.get("namespace") != plan["namespace"]:
            raise ObservationError("pod from another namespace")
        status = item.get("status") or {}
        if status.get("phase") in _FINISHED or metadata.get("deletionTimestamp"):
            continue
        labels = metadata.get("labels") or {}
        owners = [name for name, w in plan["workloads"].items() if selector_matches({"matchLabels": w["selector"]}, labels)]
        if not owners:
            outside.append(metadata.get("name"))
        for name in owners:
            grouped[name].append({
                "name": metadata.get("name"),
                "uid": metadata.get("uid"),
                "labels": labels,
                "ready": _ready(status),
                "started_at": status.get("startTime"),
                "container_started_at": _container_started(status),
                "restarts": _restarts(status),
                "host_network": (item.get("spec") or {}).get("hostNetwork") is True,
            })
    for pods in grouped.values():
        pods.sort(key=lambda p: str(p["name"]))
    return grouped, sorted(outside)


def _ready(status: dict) -> bool | None:
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return None


def _restarts(status: dict) -> int | None:
    containers = status.get("containerStatuses")
    if not containers:
        return None
    return sum(c.get("restartCount", 0) for c in containers)


def _container_started(status: dict) -> str | None:
    """지금 돌고 있는 컨테이너 중 가장 늦게 시작한 시각. 모두 UTC ISO 형식이라 글자로 비교한다."""
    started = [
        c["state"]["running"]["startedAt"]
        for c in status.get("containerStatuses") or []
        if (c.get("state") or {}).get("running", {}).get("startedAt")
    ]
    return max(started) if started else None


def probe_request(plan: dict, vantage: str) -> dict:
    """이 위치에서 재야 할 대상. 계획에 있는 대상만 나가므로 임의의 주소를 잴 수 없다."""
    workload = plan["workloads"].get(vantage)
    if workload is None:
        raise ObservationError("unknown vantage")
    targets: list[dict[str, Any]] = []
    for decision in plan["decisions"]:
        probe = decision["probe"]
        if probe is None:
            continue
        is_control = workload["role"] == "control"
        if (is_control and decision["action"] == "BLOCK") or vantage in decision["owners"]:
            targets.append({"route_id": decision["route_id"], **probe})
    if workload["role"] == "control":
        targets.extend({"check_id": c["id"], **{k: v for k, v in c.items() if k != "id"}} for c in plan["new_connection_checks"])
    return {"targets": targets}


def build_observation(
    plan: dict,
    *,
    phase: str,
    pods: dict,
    policies_start: dict,
    policies_end: dict,
    probe_runs: dict[str, dict],
    replication: list[dict],
    cutover: dict | None,
    policy_applied_at: str | None,
    induced_restarts: dict[str, int] | None = None,
) -> dict:
    """probe_runs는 {위치: {"pod_uid": ..., "results": probe.run 결과}} 형식이다.

    대조군 위치는 앞뒤로 두 번 재므로 {"control": [{...}, {...}]}처럼 목록도 받는다.
    induced_restarts는 {Pod UID: 재시작 횟수}로, 운영자가 일부러 컨테이너를 다시 띄운 기록이다.
    """
    if phase not in PHASES:
        raise ObservationError("unknown phase")
    try:
        probes, checks = _flatten_runs(probe_runs)
    except (AttributeError, KeyError, TypeError) as exc:
        raise ObservationError("probe results are malformed") from exc
    grouped, outside = _classify_pods(pods, plan)
    observation = {
        "schema_version": OBSERVATION_VERSION,
        "plan_digest": plan["digest"],
        "phase": phase,
        "pods": grouped,
        "unclassified_pods": outside,
        "policy_readback": {"start": policies_start.get("items"), "end": policies_end.get("items")},
        "probes": probes,
        "replication": replication,
        "app_checks": checks,
        "cutover": cutover,
        "induced_restarts": induced_restarts or {},
    }
    if policy_applied_at is not None:
        observation["policy_applied_at"] = policy_applied_at
    return observation


def _flatten_runs(probe_runs: dict) -> tuple[list[dict], list[dict]]:
    probes, checks = [], []
    for vantage, runs in probe_runs.items():
        for run in runs if isinstance(runs, list) else [runs]:
            for result in run["results"]:
                if "route_id" in result:
                    probes.append({
                        "route_id": result["route_id"],
                        "vantage": vantage,
                        "pod_uid": run["pod_uid"],
                        "state": result.get("state"),
                        "observed_at": result.get("observed_at"),
                    })
                else:
                    checks.append({"id": result.get("check_id"), "ok": result.get("ok"), "observed_at": result.get("observed_at")})
    return probes, checks
