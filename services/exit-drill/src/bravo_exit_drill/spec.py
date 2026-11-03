"""드릴 명세 읽기. 어떤 경로가 무슨 경로인지 사람이 선언한 입력이다.

여기서는 모양만 본다. "복제 경로를 막는 선언인가" 같은 의미 판단은 policy.py가 한다.
"""

from dataclasses import dataclass
from typing import Any

SPEC_VERSION = "exit-drill-spec/v1"
ROLES = {"app", "replication", "infrastructure", "control"}
CLASSES = {"local", "source_access", "replication"}
PROTOCOLS = {"TCP", "UDP"}
PROBE_KINDS = {"tcp", "dns"}
CHECK_KINDS = {"http", "websocket"}
EVIDENCE_KINDS = {"tcp", "stream"}
DIRECTIONS = {"egress", "ingress"}


class SpecError(ValueError):
    """명세 모양이 틀렸다. 시크릿이 없는 입력이므로 어느 필드인지 그대로 말한다."""


@dataclass(frozen=True)
class Workload:
    name: str
    selector: dict[str, str]
    role: str
    aws_client: bool = False


@dataclass(frozen=True)
class Destination:
    """같은 네임스페이스 · 다른 네임스페이스의 Pod, 또는 클러스터 밖 주소."""

    namespace: str | None
    selector: dict[str, str] | None
    host: str | None


@dataclass(frozen=True)
class Port:
    protocol: str
    port: int


@dataclass(frozen=True)
class Route:
    id: str
    klass: str
    owners: tuple[str, ...]
    to: Destination
    ports: tuple[Port, ...]
    probe: dict[str, Any] | None
    roles: tuple[str, ...]
    evidence: str | None
    # 누가 연결을 시작하는가. ingress면 to의 Pod가 owners의 Pod로 접속해 온다.
    direction: str = "egress"


@dataclass(frozen=True)
class DrillSpec:
    namespace: str
    aws_region: str
    workloads: dict[str, Workload]
    routes: tuple[Route, ...]
    new_connection_checks: tuple[dict[str, Any], ...]
    declared_residuals: tuple[str, ...]


def parse_spec(doc: Any) -> DrillSpec:
    try:
        return _parse_spec(doc)
    except TypeError as exc:  # 목록 같은 값을 이름 자리에 넣으면 집합 검사에서 터진다.
        raise SpecError("spec value has the wrong type") from exc


def _parse_spec(doc: Any) -> DrillSpec:
    _require_keys(doc, "spec", {"schema_version", "namespace", "aws_region", "workloads", "routes", "new_connection_checks"}, {"declared_residuals"})
    if doc["schema_version"] != SPEC_VERSION:
        raise SpecError("unsupported spec schema_version")
    namespace = _text(doc["namespace"], "namespace")
    checks = doc["new_connection_checks"]
    if not isinstance(checks, list) or not checks:
        raise SpecError("new_connection_checks must list at least one check")
    residuals = doc.get("declared_residuals", [])
    if not isinstance(residuals, list) or not all(isinstance(r, str) and r for r in residuals):
        raise SpecError("declared_residuals must be a list of text")
    parsed_checks = tuple(_check(c) for c in checks)
    check_ids = [c["id"] for c in parsed_checks]
    if len(set(check_ids)) != len(check_ids):
        raise SpecError("new_connection_checks ids must be unique")
    return DrillSpec(
        namespace=namespace,
        aws_region=_text(doc["aws_region"], "aws_region"),
        workloads=_workloads(doc["workloads"]),
        routes=tuple(_route(r, namespace) for r in _list(doc["routes"], "routes")),
        new_connection_checks=parsed_checks,
        declared_residuals=tuple(residuals),
    )


def _workloads(doc: Any) -> dict[str, Workload]:
    if not isinstance(doc, dict):
        raise SpecError("workloads must be an object")
    result = {}
    for name, item in doc.items():
        _require_keys(item, f"workload {name}", {"selector", "role"}, {"aws_client"})
        if item["role"] not in ROLES:
            raise SpecError(f"workload {name}: unknown role")
        if item.get("aws_client", False) not in (True, False):
            raise SpecError(f"workload {name}: aws_client must be boolean")
        result[name] = Workload(name, _labels(item["selector"], f"workload {name} selector"), item["role"], item.get("aws_client", False))
    return result


def _route(doc: Any, namespace: str) -> Route:
    _require_keys(doc, "route", {"id", "class", "owners", "to", "ports"}, {"probe", "roles", "evidence", "direction"})
    route_id = _text(doc["id"], "route id")
    klass = doc["class"]
    if klass not in CLASSES:
        raise SpecError(f"route {route_id}: unknown class")
    evidence = doc.get("evidence")
    if klass == "replication":
        if evidence not in EVIDENCE_KINDS:
            raise SpecError(f"route {route_id}: replication needs evidence tcp or stream")
    elif evidence is not None:
        raise SpecError(f"route {route_id}: evidence applies to replication only")
    direction = doc.get("direction", "egress")
    if direction not in DIRECTIONS or (direction == "ingress" and evidence != "stream"):
        raise SpecError(f"route {route_id}: ingress applies to stream evidence only")
    probe = doc.get("probe")
    if klass != "replication" or evidence == "tcp":
        probe = _probe(probe, route_id)
    elif probe is not None:
        raise SpecError(f"route {route_id}: stream evidence takes no probe")
    owners = _list(doc["owners"], f"route {route_id} owners")
    if not owners or not all(isinstance(o, str) for o in owners):
        raise SpecError(f"route {route_id}: owners must name workloads")
    ports = _list(doc["ports"], f"route {route_id} ports")
    if not ports:
        raise SpecError(f"route {route_id}: ports required")
    roles = _list(doc.get("roles", []), f"route {route_id} roles")
    if not all(isinstance(role, str) for role in roles):
        raise SpecError(f"route {route_id}: roles must be text")
    return Route(
        id=route_id,
        klass=klass,
        owners=tuple(owners),
        to=_destination(doc["to"], route_id, namespace),
        ports=tuple(_port(p, route_id) for p in ports),
        probe=probe,
        roles=tuple(roles),
        evidence=evidence,
        direction=direction,
    )


def _destination(doc: Any, route_id: str, namespace: str) -> Destination:
    _require_keys(doc, f"route {route_id} to", set(), {"namespace", "selector", "host"})
    if ("host" in doc) == ("selector" in doc):
        raise SpecError(f"route {route_id}: to needs exactly one of host or selector")
    if "host" in doc:
        if "namespace" in doc:
            raise SpecError(f"route {route_id}: host takes no namespace")
        return Destination(None, None, _text(doc["host"], f"route {route_id} host"))
    # 빈 선택자는 여기서 받아 두고 policy.py가 거부한다. 그래야 다른 위반과 함께 보고된다.
    selector = _labels(doc["selector"], f"route {route_id} selector")
    return Destination(doc.get("namespace", namespace), selector, None)


def _port(doc: Any, route_id: str) -> Port:
    _require_keys(doc, f"route {route_id} port", {"protocol", "port"}, set())
    number = doc["port"]
    if doc["protocol"] not in PROTOCOLS or type(number) is not int or not 1 <= number <= 65535:
        raise SpecError(f"route {route_id}: invalid port")
    return Port(doc["protocol"], number)


def _probe(doc: Any, route_id: str) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("kind") not in PROBE_KINDS:
        raise SpecError(f"route {route_id}: probe required")
    keys = {"kind", "host"} if doc["kind"] == "dns" else {"kind", "host", "port"}
    _require_keys(doc, f"route {route_id} probe", keys, set())
    _text(doc["host"], f"route {route_id} probe host")
    if doc["kind"] == "tcp" and (type(doc["port"]) is not int or not 1 <= doc["port"] <= 65535):
        raise SpecError(f"route {route_id}: invalid probe port")
    return dict(doc)


def _check(doc: Any) -> dict[str, Any]:
    _require_keys(doc, "new connection check", {"id", "kind", "host", "port", "path"}, set())
    _text(doc["id"], "new connection check id")
    if doc["kind"] not in CHECK_KINDS:
        raise SpecError("new connection check: unknown kind")
    if type(doc["port"]) is not int or not str(doc["path"]).startswith("/"):
        raise SpecError("new connection check: invalid address")
    return dict(doc)


def _require_keys(doc: Any, label: str, required: set[str], optional: set[str]) -> None:
    if not isinstance(doc, dict):
        raise SpecError(f"{label} must be an object")
    missing = required - doc.keys()
    if missing:
        raise SpecError(f"{label}: missing {sorted(missing)}")
    extra = doc.keys() - required - optional
    if extra:
        raise SpecError(f"{label}: unknown {sorted(extra)}")


def _labels(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise SpecError(f"{label} must map text to text")
    return dict(value)


def _list(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise SpecError(f"{label} must be a list")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SpecError(f"{label} must be text")
    return value
