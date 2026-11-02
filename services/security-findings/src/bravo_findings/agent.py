"""온프렘 수집 에이전트. 온프렘 클러스터 안에서 돌며 플랫폼으로 **바깥 방향**으로만 보낸다(설계 문서 7.5).

- Kyverno PolicyReport 결과를 읽어 보낸다 (K8s API, policyreports · clusterpolicyreports get · list만).
- Falco DaemonSet 준비 상태를 확인해 heartbeat를 보낸다. 탐지 이벤트 자체는 falcosidekick이 웹훅으로 보낸다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

POLICY_REPORT_PATHS = (
    "/apis/wgpolicyk8s.io/v1alpha2/policyreports",
    "/apis/wgpolicyk8s.io/v1alpha2/clusterpolicyreports",
)


def _timestamp(value: Any, fallback: str) -> str:
    # PolicyReport 결과의 timestamp는 {seconds, nanos}
    if isinstance(value, dict) and "seconds" in value:
        return datetime.fromtimestamp(int(value["seconds"]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fallback


def policy_report_results(reports: list[dict], now_iso: str) -> list[dict]:
    """PolicyReport 목록 → 정규화기(kyverno)가 받는 결과 목록.
    대상 자원은 결과의 resources[0], 없으면 보고서의 scope(새 Kyverno는 자원마다 보고서를 만든다)."""
    out = []
    for report in reports:
        scope = report.get("scope") or {}
        for result in report.get("results", []):
            resources = result.get("resources") or ([scope] if scope else [])
            for resource in resources or [{}]:
                out.append(
                    {
                        "policy": result.get("policy", ""),
                        "rule": result.get("rule", ""),
                        "result": result.get("result", ""),
                        "severity": result.get("severity"),
                        "message": result.get("message", ""),
                        "resource": {
                            "kind": resource.get("kind", ""),
                            "name": resource.get("name", ""),
                            "namespace": resource.get(
                                "namespace", report.get("metadata", {}).get("namespace", "")
                            ),
                        },
                        "timestamp": _timestamp(result.get("timestamp"), now_iso),
                    }
                )
    return out


def falco_ready(daemonset: dict) -> bool:
    status = daemonset.get("status", {})
    desired = status.get("desiredNumberScheduled", 0)
    return desired > 0 and status.get("numberReady", 0) == desired


def collect(
    k8s_get: Callable[[str], dict],
    now: datetime,
    falco_namespace: str = "falco",
    falco_name: str = "falco",
) -> list[dict]:
    """플랫폼 ingest API로 보낼 요청 본문 목록. k8s_get은 경로를 받아 JSON을 돌려준다(인증된 클러스터 안 클라이언트)."""
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    batches = []
    try:
        reports = [item for path in POLICY_REPORT_PATHS for item in k8s_get(path).get("items", [])]
        batches.append({"source": "kyverno", "events": policy_report_results(reports, now_iso)})
    except Exception:  # noqa: BLE001 — 읽지 못했으면 "확인 불가"로 보고한다
        batches.append({"source": "kyverno", "events": [], "agent_error": "SOURCE_UNHEALTHY"})
    try:
        ds = k8s_get(f"/apis/apps/v1/namespaces/{falco_namespace}/daemonsets/{falco_name}")
        batch: dict[str, Any] = {"source": "falco", "events": []}
        if not falco_ready(ds):
            batch["agent_error"] = "SOURCE_UNHEALTHY"
        batches.append(batch)
    except Exception:  # noqa: BLE001
        batches.append({"source": "falco", "events": [], "agent_error": "SOURCE_UNHEALTHY"})
    return batches
