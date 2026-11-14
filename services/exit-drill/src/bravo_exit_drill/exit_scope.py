"""출구 시험 차단 대상 등록부(ADR-0034 칸 `source.exit_scope`). 데이터를 고르기만 하고 접속하지 않는다.

출발지 종류마다 어댑터 하나가 "목적지 앱이 닿으면 안 되는 출발지 주소"를 만든다.

- aws: 인벤토리가 쓴다고 한 AWS 서비스의 공개 주소(`*.amazonaws.com`).
- gcp: 같은 역할의 Google API 주소(`*.googleapis.com`)와 GCS 버킷 주소.
- onprem: 인벤토리가 알려 준 출발지 내부 서비스 주소(SeaweedFS · OpenBao · 출발지 DB 등).

목적지가 같은 종류면 공통 주소를 통째로 막으면 목적지까지 막힌다. 그때는 계약의 `scope_keys`
(aws: account_id · region, gcp: project_id · region, onprem: cluster_uid)로 출발지에만 해당하는
주소만 고른다. 호스트로 출발지와 목적지를 가를 수 없는 의존성은 버리지 않고 `Indistinct`로 돌려준다.
계획은 그것을 `scope_indistinct`에 적고 판정은 UNKNOWN(`SCOPE_INDISTINCT`)으로 드러낸다(ADR-0014: 불명확하면 통과가 아니다).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from . import aws_endpoints, gcp_endpoints
from .inventory_input import Dependency, SourceResource

# docs/contracts/environment-kinds.json의 kinds.*.scope_keys · endpoint_suffixes와 같다(시험이 대조한다).
SCOPE_KEYS: dict[str, tuple[str, ...]] = {
    "aws": ("account_id", "region"),
    "gcp": ("project_id", "region"),
    "onprem": ("cluster_uid",),
}
ENDPOINT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "aws": ("amazonaws.com",),
    "gcp": ("googleapis.com",),
    "onprem": (),
}
# 주소를 만들려면 꼭 있어야 하는 범위 값. 같은 종류 간 이전이면 SCOPE_KEYS 전부가 양쪽에 필요하다.
_HOST_KEYS: dict[str, tuple[str, ...]] = {"aws": ("region",), "gcp": (), "onprem": ()}
HTTPS_PORT = 443
# 주소에 포트가 없을 때 주소 형식으로 정하는 포트. 역할별 기본 포트는 inventory_input.py가 정한다.
_SCHEME_PORTS = {"http": 80, "https": 443, "postgres": 5432, "postgresql": 5432}
_CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local", ".cluster.local")

# 왜 호스트로 가를 수 없는가. 지도와 판정에 그대로 나온다.
GLOBAL_ENDPOINT = "global_endpoint_shared_with_target"
SHARED_REGIONAL_ENDPOINT = "regional_endpoint_shared_with_target"
RESOURCE_IN_TARGET_SCOPE = "resource_in_target_scope"
CLUSTER_RELATIVE_NAME = "cluster_relative_name"
ENDPOINT_UNREADABLE = "endpoint_unreadable"


@dataclass(frozen=True)
class BlockTarget:
    """목적지 앱에서 닫혀 있어야 하는 출발지 주소 하나."""

    id: str
    host: str
    port: int
    roles: tuple[str, ...]


@dataclass(frozen=True)
class Indistinct:
    """출발지와 목적지가 같은 주소를 써서 차단을 잴 수 없는 의존성."""

    id: str
    host: str
    roles: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ExitScope:
    source_kind: str
    target_kind: str
    targets: tuple[BlockTarget, ...]
    indistinct: tuple[Indistinct, ...]
    violations: tuple[tuple[str, str], ...]  # (코드, 대상). policy.py가 Violation으로 바꿔 함께 알린다


@dataclass(frozen=True)
class _Context:
    source_kind: str
    target_kind: str
    source: Mapping[str, str]
    target: Mapping[str, str]
    dependencies: Sequence[Dependency]
    resources: Sequence[SourceResource]

    @property
    def same_kind(self) -> bool:
        return self.source_kind == self.target_kind


_Found = tuple[list[BlockTarget], list[Indistinct]]


def resolve(
    source_kind: str,
    target_kind: str,
    source_scope: Mapping[str, str],
    target_scope: Mapping[str, str],
    dependencies: Sequence[Dependency],
    resources: Sequence[SourceResource],
) -> ExitScope:
    adapter = ADAPTERS.get(source_kind)
    if adapter is None:
        return ExitScope(source_kind, target_kind, (), (), (("UNSUPPORTED_SOURCE_KIND", source_kind),))
    violations = _check_scope(source_kind, target_kind, source_scope, target_scope)
    if violations:
        # 범위를 모르면 무엇이 출발지인지 모른다. 반쯤 만든 목록으로 계획을 만들지 않는다.
        return ExitScope(source_kind, target_kind, (), (), tuple(violations))
    targets, indistinct = adapter(_Context(source_kind, target_kind, source_scope, target_scope, dependencies, resources))
    return ExitScope(
        source_kind,
        target_kind,
        tuple(sorted({t.id: t for t in targets}.values(), key=lambda t: t.id)),
        tuple(sorted({i.id: i for i in indistinct}.values(), key=lambda i: i.id)),
        (),
    )


def _check_scope(source_kind: str, target_kind: str, source: Mapping[str, str], target: Mapping[str, str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if target_kind not in SCOPE_KEYS:
        found.append(("UNSUPPORTED_TARGET_KIND", target_kind))
        return found
    found.extend(("SCOPE_KEY_UNKNOWN", f"source.{key}") for key in sorted(source) if key not in SCOPE_KEYS[source_kind])
    found.extend(("SCOPE_KEY_UNKNOWN", f"target.{key}") for key in sorted(target) if key not in SCOPE_KEYS[target_kind])
    same_kind = source_kind == target_kind
    needed = SCOPE_KEYS[source_kind] if same_kind else _HOST_KEYS[source_kind]
    found.extend(("SCOPE_KEY_MISSING", f"source.{key}") for key in needed if not source.get(key))
    if same_kind:
        found.extend(("SCOPE_KEY_MISSING", f"target.{key}") for key in SCOPE_KEYS[target_kind] if not target.get(key))
        # ADR-0034 3.2-2: 같은 종류 간 이전은 클러스터 신원이 달라야 한다. 같은 클러스터는 이전이 아니다.
        if source_kind == "onprem" and source.get("cluster_uid") and source.get("cluster_uid") == target.get("cluster_uid"):
            found.append(("SAME_CLUSTER", "cluster_uid"))
    return found


def _services(ctx: _Context) -> dict[str, tuple[str, ...]]:
    roles: dict[str, set[str]] = {}
    for dependency in ctx.dependencies:
        for service in dependency.source_services:
            roles.setdefault(service, set()).add(dependency.role)
    return {service: tuple(sorted(r)) for service, r in sorted(roles.items())}


def _buckets(ctx: _Context) -> list[SourceResource]:
    return [r for r in ctx.resources if r.is_bucket and r.name]


def _in_target_scope(ctx: _Context, resource: SourceResource, key: str) -> bool:
    """자원이 목적지 범위(계정 · 프로젝트 · 클러스터)에 있으면 출발지 주소로 막을 수 없다."""
    value = resource.attr(key)
    return bool(ctx.same_kind and value and value == ctx.target.get(key))


def _aws(ctx: _Context) -> _Found:
    region = ctx.source["region"]
    targets: list[BlockTarget] = []
    indistinct: list[Indistinct] = []
    for service, roles in _services(ctx).items():
        route_id, host = f"aws-{service}", aws_endpoints.endpoint_host(service, region)
        if not ctx.same_kind:
            targets.append(BlockTarget(route_id, host, HTTPS_PORT, roles))
        elif aws_endpoints.is_global(service):
            indistinct.append(Indistinct(route_id, host, roles, GLOBAL_ENDPOINT))
        elif ctx.target.get("region") != region:
            targets.append(BlockTarget(route_id, host, HTTPS_PORT, roles))
        else:
            indistinct.append(Indistinct(route_id, host, roles, SHARED_REGIONAL_ENDPOINT))
    if ctx.same_kind:
        # 공통 주소를 못 막을 때도 버킷 가상 호스트는 출발지 버킷에만 해당한다.
        for bucket in _buckets(ctx):
            route_id = f"aws-s3-bucket-{bucket.name}"
            host = aws_endpoints.bucket_host(bucket.name, bucket.attr("region") or region)
            if _in_target_scope(ctx, bucket, "account_id"):
                indistinct.append(Indistinct(route_id, host, (bucket.role,), RESOURCE_IN_TARGET_SCOPE))
            else:
                targets.append(BlockTarget(route_id, host, HTTPS_PORT, (bucket.role,)))
    return targets, indistinct


def _gcp(ctx: _Context) -> _Found:
    region = ctx.source.get("region", "")
    targets: list[BlockTarget] = []
    indistinct: list[Indistinct] = []
    for service, roles in _services(ctx).items():
        route_id, host = f"gcp-{service}", gcp_endpoints.global_host(service)
        if not ctx.same_kind:
            targets.append(BlockTarget(route_id, host, HTTPS_PORT, roles))
            continue
        # Google API 공통 주소는 프로젝트와 리전에 상관없이 같다. 다른 프로젝트의 목적지도 쓴다.
        indistinct.append(Indistinct(route_id, host, roles, GLOBAL_ENDPOINT))
        if gcp_endpoints.has_regional_endpoint(service) and region != ctx.target.get("region"):
            regional = gcp_endpoints.regional_host(service, region)
            targets.append(BlockTarget(f"gcp-{service}-{region}", regional, HTTPS_PORT, roles))
    for bucket in _buckets(ctx):
        route_id, host = f"gcp-gcs-bucket-{bucket.name}", gcp_endpoints.bucket_host(bucket.name)
        if _in_target_scope(ctx, bucket, "project_id"):
            indistinct.append(Indistinct(route_id, host, (bucket.role,), RESOURCE_IN_TARGET_SCOPE))
        else:
            targets.append(BlockTarget(route_id, host, HTTPS_PORT, (bucket.role,)))
    return targets, indistinct


def _onprem(ctx: _Context) -> _Found:
    targets: list[BlockTarget] = []
    indistinct: list[Indistinct] = []
    for resource in ctx.resources:
        endpoint = resource.endpoint
        if not endpoint:
            continue
        route_id, roles = f"onprem-{resource.role}-{resource.name}", (resource.role,)
        host, port = split_endpoint(endpoint, resource.default_port)
        if not host or port is None:
            indistinct.append(Indistinct(route_id, endpoint, roles, ENDPOINT_UNREADABLE))
        elif _in_target_scope(ctx, resource, "cluster_uid"):
            indistinct.append(Indistinct(route_id, host, roles, RESOURCE_IN_TARGET_SCOPE))
        elif is_cluster_relative(host):
            # `*.svc` 같은 이름은 목적지 클러스터 안에서 목적지 자신의 서비스로 풀린다. 출발지를 가리킨다고 말할 수 없다.
            indistinct.append(Indistinct(route_id, host, roles, CLUSTER_RELATIVE_NAME))
        else:
            targets.append(BlockTarget(route_id, host, port, roles))
    return targets, indistinct


def split_endpoint(endpoint: str, default_port: int | None = None) -> tuple[str, int | None]:
    """`https://host:port/...` · `host:port` · `host`를 (host, port)로. 못 읽으면 ("", None)."""
    scheme, _, rest = endpoint.strip().rpartition("://")
    authority = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    host, colon, port_text = authority.rpartition(":") if ":" in authority else (authority, "", "")
    if "[" in authority or "]" in authority:  # IPv6 주소는 지금 쓰지 않는다. 지어내지 않고 못 읽음으로 둔다.
        return "", None
    port: int | None = None
    if colon:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            return "", None
        port = int(port_text)
    else:
        port = _SCHEME_PORTS.get(scheme.lower()) or default_port
    return host.lower(), port


def is_cluster_relative(host: str) -> bool:
    return "." not in host or host.endswith(_CLUSTER_SUFFIXES)


ADAPTERS: dict[str, Callable[[_Context], _Found]] = {"aws": _aws, "gcp": _gcp, "onprem": _onprem}
