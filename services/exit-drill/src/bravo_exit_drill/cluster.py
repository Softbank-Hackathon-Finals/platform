"""`kubectl get pods,networkpolicies -o json` 결과를 해석한다. 클러스터에 접속하지 않는다.

NetworkPolicy는 합집합이다. Pod를 고르는 정책 중 하나라도 허용하면 그 경로는 열린다.
그래서 새 정책을 더하는 것만으로 차단이 되지 않고, 이미 있는 허용을 함께 봐야 한다.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

FINISHED_PHASES = {"Succeeded", "Failed"}
NAMESPACE_NAME_LABEL = "kubernetes.io/metadata.name"


class SnapshotError(ValueError):
    """클러스터 읽기 결과를 해석할 수 없다. 추측하지 않고 멈춘다."""


@dataclass(frozen=True)
class PodView:
    name: str
    uid: str
    labels: dict[str, str]
    host_network: bool
    containers: tuple[str, ...] = ()


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
    try:
        return _read_items(doc["items"], namespace)
    except SnapshotError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("snapshot item is malformed") from exc


def _read_items(items: list, namespace: str) -> Snapshot:
    pods, policies = [], []
    for item in items:
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
                    containers=tuple(c["name"] for c in (item.get("spec") or {}).get("containers") or []),
                ))
        else:
            raise SnapshotError("snapshot contains an unexpected kind")
    pods.sort(key=lambda p: p.name)
    policies.sort(key=lambda p: p["metadata"]["name"])
    digest = canonical_digest({
        "pods": [[p.name, p.uid, p.labels, p.host_network, p.containers] for p in pods],
        "policies": [[p["metadata"]["name"], p["spec"]] for p in policies],
    })
    return Snapshot(tuple(pods), tuple(policies), digest)


def _validate_policy(policy: dict) -> None:
    """알 수 없는 선택자 연산은 나중에 '안 고른다'로 잘못 읽히지 않게 여기서 멈춘다."""
    spec = policy.get("spec")
    if not isinstance(spec, dict):
        raise SnapshotError("NetworkPolicy without spec")
    selectors = [spec.get("podSelector") or {}]
    rules = [*(spec.get("egress") or []), *(spec.get("ingress") or [])]
    for rule in rules:
        for peer in [*(rule.get("to") or []), *(rule.get("from") or [])]:
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


def limits(policy: dict, direction: str) -> bool:
    """이 정책이 해당 방향을 제한하는가. direction은 egress 또는 ingress다.

    policyTypes를 생략하면 쿠버네티스는 수신은 항상, 송신은 egress 규칙이 있을 때만 제한한다.
    """
    spec = policy["spec"]
    types = spec.get("policyTypes")
    if types:
        return direction.capitalize() in types
    return direction == "ingress" or bool(spec.get("egress"))


def policies_selecting(policies, labels: dict[str, str], direction: str) -> list[dict]:
    return [p for p in policies if limits(p, direction) and selector_matches(p["spec"].get("podSelector") or {}, labels)]


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
    for policy in policies_selecting(policies, labels, "egress"):
        if policy["metadata"]["name"] in skip_names:
            continue
        if not all(rule_is_covered(rule, allowed_rules) for rule in policy["spec"].get("egress") or []):
            names.append(policy["metadata"]["name"])
    return names


def _names_namespace(selector: dict, namespace: str) -> bool:
    """이 선택자가 그 네임스페이스를 고른다고 확실히 말할 수 있는가.

    스냅샷에는 네임스페이스의 이름만 있다. 다른 레이블을 보는 선택자는 알 수 없으니 아니라고 답한다.
    """
    labels = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or []
    if not labels and not expressions:
        return True
    if any(key != NAMESPACE_NAME_LABEL for key in labels):
        return False
    if any(e.get("key") != NAMESPACE_NAME_LABEL or e.get("operator") != "In" for e in expressions):
        return False
    return selector_matches(selector, {NAMESPACE_NAME_LABEL: namespace})


def peer_reaches_pods(peer: dict, namespace: str, labels: dict[str, str]) -> bool:
    """이 peer가 해당 네임스페이스의 Pod를 확실히 가리키는가. ipBlock은 Pod를 특정하지 못한다."""
    if "ipBlock" in peer:
        return False
    if "namespaceSelector" in peer and not _names_namespace(peer["namespaceSelector"], namespace):
        return False
    return selector_matches(peer.get("podSelector") or {}, labels)


def _rule_admits_port(rule: dict, ports: list[tuple[str, int]]) -> bool:
    """수신 규칙의 포트가 이 경로를 받아 주는가. 이름 붙은 포트는 Pod 스펙 없이는 알 수 없어 아니라고 본다."""
    allowed = rule.get("ports")
    if not allowed:
        return True
    for protocol, port in ports:
        for entry in allowed:
            number, last = entry.get("port"), entry.get("endPort", entry.get("port"))
            if entry.get("protocol", "TCP") == protocol and type(number) is int and type(last) is int and number <= port <= last:
                return True
    return False


def path_open(
    policies,
    labels: dict[str, str],
    own_namespace: str,
    namespace: str,
    peer_labels: dict[str, str],
    direction: str,
    ports: list[tuple[str, int]],
) -> bool:
    """정책 전체로 볼 때 이 Pod와 상대 Pod 사이에 해당 방향의 길이 열려 있는가.

    포트는 수신에서만 본다. 송신의 상대인 tailnet 프록시는 Pod 포트를 Tailscale operator가 고르므로
    (executor-network README) 선언한 5432와 다르다. 그쪽 포트가 맞는지는 실제 접속 시험으로 잰다.
    """
    selecting = policies_selecting(policies, labels, direction)
    if not selecting:
        return True  # 이 방향을 제한하는 정책이 없으면 기본 허용이다.
    peers_key = "to" if direction == "egress" else "from"
    for policy in selecting:
        for rule in policy["spec"].get(direction) or []:
            if direction == "ingress" and not _rule_admits_port(rule, ports):
                continue
            peers = rule.get(peers_key)
            if not peers:
                return True
            for peer in peers:
                if "namespaceSelector" not in peer and namespace != own_namespace:
                    continue
                if peer_reaches_pods(peer, namespace, peer_labels):
                    return True
    return False
