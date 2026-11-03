"""`kubectl get pods,networkpolicies -o json` 결과를 해석한다. 클러스터에 접속하지 않는다.

NetworkPolicy는 합집합이다. Pod를 고르는 정책 중 하나라도 허용하면 그 경로는 열린다.
그래서 새 정책을 더하는 것만으로 차단이 되지 않고, 이미 있는 허용을 함께 봐야 한다.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

FINISHED_PHASES = {"Succeeded", "Failed"}


class SnapshotError(ValueError):
    """클러스터 읽기 결과를 해석할 수 없다. 추측하지 않고 멈춘다."""


@dataclass(frozen=True)
class PodView:
    name: str
    uid: str
    labels: dict[str, str]
    host_network: bool


@dataclass(frozen=True)
class Snapshot:
    pods: tuple[PodView, ...]
    policies: tuple[dict[str, Any], ...]
    digest: str


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def parse_snapshot(doc: Any, *, namespace: str) -> Snapshot:
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise SnapshotError("snapshot must be a Kubernetes List")
    pods, policies = [], []
    for item in doc["items"]:
        metadata = item.get("metadata") or {}
        if metadata.get("namespace") != namespace:
            raise SnapshotError("snapshot contains another namespace")
        kind = item.get("kind")
        if kind == "NetworkPolicy":
            _validate_policy(item)
            policies.append(item)
        elif kind == "Pod":
            if (item.get("status") or {}).get("phase") not in FINISHED_PHASES and not metadata.get("deletionTimestamp"):
                pods.append(PodView(
                    name=metadata["name"],
                    uid=metadata.get("uid", ""),
                    labels=dict(metadata.get("labels") or {}),
                    host_network=(item.get("spec") or {}).get("hostNetwork") is True,
                ))
        else:
            raise SnapshotError("snapshot contains an unexpected kind")
    pods.sort(key=lambda p: p.name)
    policies.sort(key=lambda p: p["metadata"]["name"])
    digest = canonical_digest({
        "pods": [[p.name, p.uid, p.labels, p.host_network] for p in pods],
        "policies": [[p["metadata"]["name"], p["spec"]] for p in policies],
    })
    return Snapshot(tuple(pods), tuple(policies), digest)


def _validate_policy(policy: dict) -> None:
    """알 수 없는 선택자 연산은 나중에 '안 고른다'로 잘못 읽히지 않게 여기서 멈춘다."""
    spec = policy.get("spec")
    if not isinstance(spec, dict):
        raise SnapshotError("NetworkPolicy without spec")
    selectors = [spec.get("podSelector") or {}]
    for rule in spec.get("egress") or []:
        for peer in rule.get("to") or []:
            selectors.extend(peer[key] for key in ("podSelector", "namespaceSelector") if key in peer)
    for selector in selectors:
        selector_matches(selector, {})


def selector_matches(selector: dict, labels: dict[str, str]) -> bool:
    """쿠버네티스 LabelSelector 판정. 빈 선택자는 전부 고른다."""
    matched = all(labels.get(k) == v for k, v in (selector.get("matchLabels") or {}).items())
    for expression in selector.get("matchExpressions") or []:
        key, operator = expression.get("key"), expression.get("operator")
        values = expression.get("values") or []
        if operator == "In":
            matched = matched and labels.get(key) in values
        elif operator == "NotIn":
            matched = matched and labels.get(key) not in values
        elif operator == "Exists":
            matched = matched and key in labels
        elif operator == "DoesNotExist":
            matched = matched and key not in labels
        else:
            raise SnapshotError("unknown selector operator")
    return matched


def limits_egress(policy: dict) -> bool:
    """policyTypes를 생략하면 egress 칸이 있을 때만 송신 정책이다."""
    spec = policy["spec"]
    types = spec.get("policyTypes")
    return "Egress" in types if types else "egress" in spec


def egress_policies_selecting(policies, labels: dict[str, str]) -> list[dict]:
    return [p for p in policies if limits_egress(p) and selector_matches(p["spec"].get("podSelector") or {}, labels)]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _canonical_ports(rule: dict) -> set[str]:
    return {_canonical(p) for p in rule.get("ports") or []}


def rule_is_covered(rule: dict, allowed_rules: list[dict]) -> bool:
    """이 허용 규칙이 계획의 허용 안에 그대로 들어가는가.

    대상을 넓게 해석하지 않고 같은 peer만 인정한다. to나 ports를 비운 규칙은 전부 허용이라
    덮이지 않는다. 그 결과 기존 허용을 전부 명세에 선언해야 통과한다.
    """
    peers, ports = rule.get("to"), _canonical_ports(rule)
    if not peers or not ports:
        return False
    for peer in peers:
        candidates = [a for a in allowed_rules if _canonical(peer) in {_canonical(p) for p in a["to"]}]
        if not any(ports <= _canonical_ports(a) for a in candidates):
            return False
    return True


def undeclared_egress(policies, labels: dict[str, str], allowed_rules: list[dict], *, skip_names=frozenset()) -> list[str]:
    """이 Pod에 계획 밖의 허용을 주는 정책 이름들."""
    names = []
    for policy in egress_policies_selecting(policies, labels):
        if policy["metadata"]["name"] in skip_names:
            continue
        if not all(rule_is_covered(rule, allowed_rules) for rule in policy["spec"].get("egress") or []):
            names.append(policy["metadata"]["name"])
    return names


def peer_reaches_pods(peer: dict, namespace: str, labels: dict[str, str]) -> bool:
    """이 peer가 해당 네임스페이스의 Pod를 확실히 가리키는가. ipBlock은 Pod를 특정하지 못한다."""
    if "ipBlock" in peer:
        return False
    if "namespaceSelector" in peer:
        if not selector_matches(peer["namespaceSelector"], {"kubernetes.io/metadata.name": namespace}):
            return False
    return selector_matches(peer.get("podSelector") or {}, labels)


def egress_allows_pods(policies, source_labels: dict[str, str], own_namespace: str, namespace: str, labels: dict[str, str]) -> bool:
    """정책 전체로 볼 때 이 Pod에서 대상 Pod로 가는 길이 열려 있는가. 포트는 보지 않는다.

    tailnet 프록시의 실제 포트는 운영자가 고른 번호라(executor-network README) 선언한 5432와
    다르다. 포트까지 맞는지는 실제 접속 시험으로 잰다.
    """
    selecting = egress_policies_selecting(policies, source_labels)
    if not selecting:
        return True  # 송신 정책이 없는 Pod는 기본 허용이다.
    for policy in selecting:
        for rule in policy["spec"].get("egress") or []:
            peers = rule.get("to")
            if not peers:
                return True
            for peer in peers:
                if "namespaceSelector" not in peer and namespace != own_namespace:
                    continue
                if peer_reaches_pods(peer, namespace, labels):
                    return True
    return False
