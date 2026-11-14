"""차단 정책 생성기. 입력만으로 무엇을 막고 무엇을 남길지 정하고 NetworkPolicy를 만든다.

방식은 허용 목록이다. 앱과 풀러 Pod의 송신을 선언한 목적지 내부 경로로만 좁히면, AWS API와
tailnet을 거쳐 출발지로 가는 길은 이름을 몰라도 전부 닫힌다. 차단 목록은 AWS 주소가 자주
바뀌어 빠뜨리기 쉽다. DB Pod는 고르지 않으므로 복제 채널에는 손대지 않는다.

위험한 입력은 일부러 만들지 않고 실패한다. 적용과 삭제는 이 도구의 일이 아니다.
"""

from dataclasses import asdict, dataclass

from .cluster import (
    Snapshot,
    canonical_digest,
    path_open,
    selector_matches,
    undeclared_egress,
)
from .exit_scope import ExitScope, resolve
from .inventory_input import InventoryView
from .spec import Destination, DrillSpec, Port, Route, Workload

PLAN_VERSION = "exit-drill-plan/v1"
TAILNET_NAMESPACE = "tailscale-system"
TAILNET_LABEL_PREFIX = "tailscale.com/"
POSTGRES = Port("TCP", 5432)
HTTPS = Port("TCP", 443)
POLICY_PREFIX = "exit-drill-egress-"
DIGEST_ANNOTATION = "bravo.exit-drill/plan-digest"
_DIGEST_FIELDS = (
    "namespace",
    "candidate",
    "inventory_digest",
    "workloads",
    "decisions",
    "manifests",
    "new_connection_checks",
    "unmapped_dependencies",
    "declared_residuals",
)
# 같은 종류 간 이전처럼 호스트로 가를 수 없는 의존성이 있을 때만 계획에 생기는 칸. 있으면 해시에 들어간다.
SCOPE_FIELD = "scope_indistinct"
ACTIONS = {"local": "ALLOW", "source_access": "BLOCK", "replication": "KEEP"}
REASONS = {
    "ALLOW": "목적지 안의 경로라서 허용 목록에 넣는다",
    "BLOCK": "출발지로 가는 경로라서 허용 목록에서 뺀다",
    "KEEP": "복제 채널이라서 이 경로를 쓰는 Pod를 정책으로 고르지 않는다",
}


@dataclass(frozen=True)
class Violation:
    code: str
    subject: str


class PlanRefused(Exception):
    """계획을 만들면 복제가 끊기거나 AWS 경로가 남는다. 모든 위반을 함께 알린다."""

    def __init__(self, violations: list[Violation]):
        super().__init__(", ".join(f"{v.code}:{v.subject}" for v in violations))
        self.violations = violations


def build_plan(spec: DrillSpec, inventory: InventoryView, snapshot: Snapshot) -> dict:
    scope = exit_scope_for(spec, inventory)
    routes = _routes_with_source(spec, scope)
    manifests = _render_manifests(spec, routes)
    violations = [
        *[Violation(code, subject) for code, subject in scope.violations],
        *_check_workloads(spec, inventory, scope),
        *_check_routes(spec, routes),
        *_check_pods(spec, snapshot),
        *_check_existing_policies(spec, routes, manifests, snapshot),
    ]
    if violations:
        raise PlanRefused(sorted(violations, key=lambda v: (v.code, v.subject)))
    core = {
        "namespace": spec.namespace,
        "candidate": inventory.candidate,
        "inventory_digest": inventory.digest,
        "workloads": {w.name: _workload(w) for w in sorted(spec.workloads.values(), key=lambda w: w.name)},
        "decisions": [_decision(r) for r in routes],
        "manifests": manifests,
        "new_connection_checks": sorted(spec.new_connection_checks, key=lambda c: c["id"]),
        "unmapped_dependencies": _unmapped(inventory, routes, scope),
        "declared_residuals": sorted(spec.declared_residuals),
    }
    if scope.indistinct:
        core[SCOPE_FIELD] = [
            {"id": i.id, "host": i.host, "roles": list(i.roles), "reason": i.reason,
             "source_kind": scope.source_kind, "target_kind": scope.target_kind}
            for i in scope.indistinct
        ]
    digest = compute_digest(core)
    for manifest in core["manifests"]:
        manifest["metadata"]["annotations"] = {DIGEST_ANNOTATION: digest}
    return {"schema_version": PLAN_VERSION, **core, "digest": digest, "snapshot_digest": snapshot.digest}


def _workload(workload: Workload) -> dict:
    item = {"selector": workload.selector, "role": workload.role, "container": workload.container}
    if workload.role == "edge":
        # edge가 없는 명세의 계획은 전과 같은 모양 · 같은 해시로 둔다.
        item["residual"] = workload.residual
    return item


def compute_digest(plan: dict) -> str:
    """계획의 해시. Pod가 재시작해도 같은 계획이어야 하므로 스냅샷 해시는 넣지 않는다.

    정책에 붙이는 해시 주석 자신도 뺀다.
    """
    core = {key: plan.get(key) for key in _DIGEST_FIELDS}
    if SCOPE_FIELD in plan:
        core[SCOPE_FIELD] = plan[SCOPE_FIELD]
    core["manifests"] = [
        {**m, "metadata": {k: v for k, v in m["metadata"].items() if k != "annotations"}}
        for m in plan.get("manifests") or []
    ]
    return canonical_digest(core)


def plan_is_intact(plan: dict) -> bool:
    """손으로 고치거나 망가진 계획은 쓰지 않는다."""
    try:
        return compute_digest(plan) == plan.get("digest")
    except (AttributeError, KeyError, TypeError):
        return False


def exit_scope_for(spec: DrillSpec, inventory: InventoryView) -> ExitScope:
    """출발지 종류의 등록부로 차단 대상을 고른다. 범위 값은 명세, 종류와 자원은 인벤토리에서 온다."""
    return resolve(
        inventory.source_kind,
        inventory.target_kind,
        spec.source_scope,
        spec.target_scope,
        inventory.dependencies,
        inventory.resources,
    )


def _routes_with_source(spec: DrillSpec, scope: ExitScope) -> list[Route]:
    """등록부가 고른 출발지 주소마다 차단 경로를 하나씩 만든다. 출발지 클라우드 API를 부르는 Pod에서 잰다."""
    clients = tuple(sorted(w.name for w in spec.workloads.values() if w.aws_client))
    derived = [
        Route(
            id=target.id,
            klass="source_access",
            owners=clients,
            to=Destination(None, None, target.host),
            ports=(HTTPS if target.port == HTTPS.port else Port("TCP", target.port),),
            probe={"kind": "tcp", "host": target.host, "port": target.port},
            roles=target.roles,
            evidence=None,
        )
        for target in scope.targets
    ]
    return sorted([*spec.routes, *derived], key=lambda r: r.id)


def _peer(route: Route, namespace: str) -> dict:
    peer: dict = {"podSelector": {"matchLabels": route.to.selector}}
    if route.to.namespace != namespace:
        peer = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": route.to.namespace}}, **peer}
    return peer


def _render_manifests(spec: DrillSpec, routes: list[Route]) -> list[dict]:
    """앱 역할 Pod마다 정책 하나. 송신만 다루므로 복제의 수신 경로와 앱의 수신은 그대로다."""
    manifests = []
    for workload in sorted(spec.workloads.values(), key=lambda w: w.name):
        if workload.role != "app":
            continue
        rules = [
            {
                "to": [_peer(r, spec.namespace)],
                "ports": [asdict(p) for p in sorted(r.ports, key=lambda p: (p.protocol, p.port))],
            }
            for r in routes
            if r.klass == "local" and workload.name in r.owners and r.to.selector
        ]
        manifests.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": POLICY_PREFIX + workload.name,
                "namespace": spec.namespace,
                "labels": {"component": "target", "app.kubernetes.io/part-of": "bravo-exit-drill"},
            },
            "spec": {
                "podSelector": {"matchLabels": workload.selector},
                "policyTypes": ["Egress"],
                "egress": rules,
            },
        })
    return manifests


def _decision(route: Route) -> dict:
    action = ACTIONS[route.klass]
    return {
        "route_id": route.id,
        "class": route.klass,
        "action": action,
        "reason": REASONS[action],
        "owners": sorted(route.owners),
        "to": asdict(route.to),
        "ports": [asdict(p) for p in sorted(route.ports, key=lambda p: (p.protocol, p.port))],
        "roles": sorted(route.roles),
        "evidence": route.evidence,
        "direction": route.direction,
        "probe": route.probe,
    }


def _unmapped(inventory: InventoryView, routes: list[Route], scope: ExitScope) -> list[dict]:
    """차단을 잴 경로가 없는 의존성. 빼지 않고 적어 두면 판정이 UNKNOWN이 된다.

    복제 경로에 역할을 적어도 소용없다. 거기는 막히는지가 아니라 열려 있는지를 잰다.
    범위로 구분 불가(scope_indistinct)로 이미 드러낸 역할은 여기 다시 적지 않는다.
    """
    covered = {role for r in routes if r.klass == "source_access" for role in r.roles}
    covered |= {role for item in scope.indistinct for role in item.roles}
    return [
        {"id": d.id, "role": d.role, "services": list(d.services)}
        for d in inventory.dependencies
        if d.role not in covered
    ]


def _by_role(spec: DrillSpec, role: str) -> list[Workload]:
    return [w for w in spec.workloads.values() if w.role == role]


def _check_workloads(spec: DrillSpec, inventory: InventoryView, scope: ExitScope) -> list[Violation]:
    found = [Violation("EMPTY_SELECTOR", w.name) for w in spec.workloads.values() if not w.selector]
    if not _by_role(spec, "control"):
        found.append(Violation("NO_CONTROL", "control"))
    needs_client = bool(scope.targets)
    if needs_client and not any(w.aws_client for w in spec.workloads.values()):
        found.append(Violation("NO_AWS_CLIENT", "workloads"))
    listed = {*inventory.residuals, *spec.declared_residuals}
    found.extend(Violation("UNKNOWN_RESIDUAL", w.name) for w in _by_role(spec, "edge") if w.residual not in listed)
    return found


def _check_routes(spec: DrillSpec, routes: list[Route]) -> list[Violation]:
    found: list[Violation] = []
    seen: set[str] = set()
    replication = [r for r in routes if r.klass == "replication"]
    if not replication:
        found.append(Violation("NO_REPLICATION_ROUTE", "routes"))
    if not any(r.klass == "source_access" for r in routes):
        found.append(Violation("NO_SOURCE_ROUTE", "routes"))
    for route in routes:
        if route.id in seen:
            found.append(Violation("DUPLICATE_ROUTE", route.id))
        seen.add(route.id)
        found.extend(_check_owners(spec, route))
        if route.klass == "local" and _leaves_cluster(route.to):
            found.append(Violation("LOCAL_ROUTE_LEAVES_CLUSTER", route.id))
        if route.klass == "replication":
            if not (route.to.selector and route.to.namespace == TAILNET_NAMESPACE):
                found.append(Violation("REPLICATION_NOT_TAILNET", route.id))
            if route.ports != (POSTGRES,):
                found.append(Violation("REPLICATION_NOT_POSTGRES", route.id))
        if route.klass == "source_access":
            if any(_same_path(route, keep) for keep in replication):
                found.append(Violation("BLOCK_HITS_REPLICATION", route.id))
            if any(_also_allowed(route, allow) for allow in routes if allow.klass == "local"):
                found.append(Violation("BLOCK_ALSO_ALLOWED", route.id))
    return found


def _check_owners(spec: DrillSpec, route: Route) -> list[Violation]:
    needed = "replication" if route.klass == "replication" else "app"
    found = []
    for owner in route.owners:
        workload = spec.workloads.get(owner)
        if workload is None:
            found.append(Violation("UNKNOWN_WORKLOAD", f"{route.id}:{owner}"))
        elif workload.role != needed:
            found.append(Violation("OWNER_ROLE_MISMATCH", f"{route.id}:{owner}"))
    return found


def _leaves_cluster(to: Destination) -> bool:
    if not to.selector or to.namespace == TAILNET_NAMESPACE:
        return True
    return any(key.startswith(TAILNET_LABEL_PREFIX) for key in to.selector)


def _same_path(route: Route, keep: Route) -> bool:
    if route.to == keep.to:
        return True
    return bool(route.probe and keep.probe and route.probe == keep.probe)


def _also_allowed(block: Route, allow: Route) -> bool:
    """같은 Pod에게 막는다면서 허용 목록에 넣은 목적지인가. 허용이 이기므로 막히지 않는다."""
    if not set(block.owners) & set(allow.owners) or not (block.to.selector and allow.to.selector):
        return False
    if block.to.namespace != allow.to.namespace:
        return False
    blocked, allowed = block.to.selector.items(), allow.to.selector.items()
    return blocked <= allowed or allowed <= blocked


def _check_pods(spec: DrillSpec, snapshot: Snapshot) -> list[Violation]:
    found: list[Violation] = []
    for workload in spec.workloads.values():
        # 터널(edge)은 시험 대상이 아니다. 내려가 있어도 새 연결 시험은 Service로 바로 간다.
        if workload.role in ("infrastructure", "edge") or not workload.selector:
            continue
        if not any(selector_matches({"matchLabels": workload.selector}, p.labels) for p in snapshot.pods):
            found.append(Violation("SELECTOR_MATCHES_NOTHING", workload.name))
        if workload.role == "app":
            found.extend(Violation("CONTAINER_NOT_IN_POD", f"{workload.name}:{p.name}") for p in _pods_of(workload, snapshot) if workload.container not in p.containers)
    for pod in snapshot.pods:
        owners = [w for w in spec.workloads.values() if w.selector and selector_matches({"matchLabels": w.selector}, pod.labels)]
        if not owners:
            found.append(Violation("UNCLASSIFIED_POD", pod.name))
        roles = {w.role for w in owners}
        if "app" in roles:
            if "replication" in roles:
                found.append(Violation("REPLICATION_OWNER_SELECTED", pod.name))
            if "control" in roles:
                found.append(Violation("CONTROL_SELECTED", pod.name))
            if "edge" in roles:
                # 송신을 좁히면 터널이 Cloudflare와 앱 어느 쪽에도 못 닿아 공개 경로가 끊긴다.
                found.append(Violation("EDGE_SELECTED", pod.name))
            if pod.host_network:
                found.append(Violation("HOST_NETWORK_POD", pod.name))
    return found


def _check_existing_policies(spec: DrillSpec, routes: list[Route], manifests: list[dict], snapshot: Snapshot) -> list[Violation]:
    found: list[Violation] = []
    ours = {m["metadata"]["name"]: m for m in manifests}
    for policy in snapshot.policies:
        mine = ours.get(policy["metadata"]["name"])
        if mine and policy["spec"] != mine["spec"]:
            found.append(Violation("POLICY_NAME_CONFLICT", policy["metadata"]["name"]))
    for workload in _by_role(spec, "app"):
        allowed = ours[POLICY_PREFIX + workload.name]["spec"]["egress"]
        for pod in _pods_of(workload, snapshot):
            for name in undeclared_egress(snapshot.policies, pod.labels, allowed, skip_names=ours.keys()):
                found.append(Violation("UNDECLARED_EGRESS", f"{name}:{pod.name}"))
    for route in routes:
        if route.klass != "replication" or not route.to.selector:
            continue
        for owner in route.owners:
            workload = spec.workloads.get(owner)
            if workload is None or workload.role != "replication":
                continue
            for pod in _pods_of(workload, snapshot):
                ports = [(p.protocol, p.port) for p in route.ports]
                if not path_open(snapshot.policies, pod.labels, spec.namespace, route.to.namespace, route.to.selector, route.direction, ports):
                    found.append(Violation("REPLICATION_BLOCKED", f"{route.id}:{pod.name}"))
    return found


def _pods_of(workload: Workload, snapshot: Snapshot):
    if not workload.selector:
        return []
    return [p for p in snapshot.pods if selector_matches({"matchLabels": workload.selector}, p.labels)]
