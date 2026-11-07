"""읽은 결과를 판정기 입력(관측 파일)으로 묶는다. 값을 해석하거나 고치지 않는다.

모르는 값은 모른다고 둔다. Ready 조건이 없으면 False가 아니라 None이고, 판정기가 UNKNOWN으로 본다.
"""

from typing import Any

from .cluster import selector_matches
from .verdict import OBSERVATION_VERSION

PHASES = {"before", "after"}
_FINISHED = {"Succeeded", "Failed"}
# 개발 측정용 호환 구성을 T30 렌더러가 표시하는 annotation(ADR-0017).
NOT_EXIT_EVIDENCE_ANNOTATION = "bravo.t30/not-exit-evidence"
# boto3가 호출 대상을 바꾸는 환경 변수. AWS_ENDPOINT_URL과 AWS_ENDPOINT_URL_<서비스>.
AWS_ENDPOINT_OVERRIDE = "AWS_ENDPOINT_URL"
# demo-chat 로그인이 믿는 발급자(demo-app #28). 값에 AWS 주소가 있으면 로그인이 아직 Cognito에 묶여 있다.
TRUSTED_ISSUER_VARIABLES = ("AUTH_ISSUER", "AUTH_ALSO_TRUST")
AWS_IDENTITY_HOSTS = (".amazonaws.com", ".amazoncognito.com")


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
                "host_network": (item.get("spec") or {}).get("hostNetwork") is True,
                "exit_blockers": _exit_blockers(metadata, item.get("spec")),
                **_target_container(status, plan["workloads"][name].get("container")),
            })
    for pods in grouped.values():
        pods.sort(key=lambda p: str(p["name"]))
    return grouped, sorted(outside)


def _ready(status: dict) -> bool | None:
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return None


def _exit_blockers(metadata: dict, spec: Any) -> list[str] | None:
    """이 Pod로 출구를 말할 수 없는 이유. 읽지 못하면 None이고, 판정기가 UNKNOWN으로 본다.

    표지만 보면 지우는 것으로 통과한다. AWS 호출을 돌리는 설정이 남아 있으면 차단을 잰 것이 아니므로
    그것도 따로 본다. 재시작 가능한 사이드카는 init 컨테이너 목록에 있다.
    envFrom은 Pod만 봐서는 어떤 변수가 들어오는지 모른다. 참조한 ConfigMap · Secret을 새로 읽지 않고 모른다고 둔다.
    """
    annotations = metadata.get("annotations", {})
    if not isinstance(annotations, dict) or not isinstance(spec, dict):
        return None
    containers, sidecars = spec.get("containers"), spec.get("initContainers", [])
    if not containers or not isinstance(containers, list) or not isinstance(sidecars, list):
        return None
    names, issuers = [], []
    for container in [*containers, *sidecars]:
        env = container.get("env", []) if isinstance(container, dict) else None
        if not isinstance(env, list) or not all(isinstance(e, dict) and isinstance(e.get("name"), str) for e in env):
            return None
        if container.get("envFrom", []) != []:
            return None
        for entry in env:
            names.append(entry["name"])
            if entry["name"] in TRUSTED_ISSUER_VARIABLES:
                # 발급자를 참조로 넣으면 값을 새로 읽지 않는다. envFrom과 같이 모른다고 둔다.
                if not isinstance(entry.get("value"), str):
                    return None
                issuers.append(entry["value"])
    blockers = []
    if NOT_EXIT_EVIDENCE_ANNOTATION in annotations:
        blockers.append("NOT_EXIT_EVIDENCE")
    if any(name.startswith(AWS_ENDPOINT_OVERRIDE) for name in names):
        blockers.append("AWS_ENDPOINT_REDIRECTED")
    # 앱은 AWS 없이 뜨지만(로그인 확인은 쓰기 때 한다) Cognito 토큰을 받는 한 쓰기는 AWS 공개 키에 묶여 있다.
    if any(host in value.lower() for value in issuers for host in AWS_IDENTITY_HOSTS):
        blockers.append("AWS_IDENTITY_TRUSTED")
    return blockers


def _target_container(status: dict, wanted: str | None) -> dict:
    """계획이 정한 컨테이너 하나의 상태. 없거나 같은 이름이 둘이면 모두 None으로 둔다.

    다른 컨테이너의 재시작은 앱이 다시 떴다는 근거가 아니다. 횟수만 따로 적어 판정이 보게 한다.
    재시작 가능한 사이드카는 init 컨테이너 목록에 나오므로 함께 센다.
    """
    containers = status.get("containerStatuses") or []
    matches = [c for c in containers if c.get("name") == wanted]
    if wanted is None or len(matches) != 1:
        return dict.fromkeys(("container_name", "container_id", "previous_container_id", "container_started_at", "restarts", "other_restarts"))
    target = matches[0]
    others = [c.get("restartCount") for c in [*containers, *(status.get("initContainerStatuses") or [])] if c.get("name") != wanted]
    return {
        "container_name": wanted,
        "container_id": target.get("containerID"),
        "previous_container_id": ((target.get("lastState") or {}).get("terminated") or {}).get("containerID"),
        "container_started_at": ((target.get("state") or {}).get("running") or {}).get("startedAt"),
        "restarts": target.get("restartCount"),
        "other_restarts": sum(others) if all(type(r) is int for r in others) else None,
    }


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
    induced_restarts는 운영자가 일부러 컨테이너를 다시 띄운 기록이다. Pod마다 한 번이고, 형식은
    {Pod UID: {"container": 컨테이너 이름, "replaced_container_id": 죽이기 전의 컨테이너 ID}}이다.
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
