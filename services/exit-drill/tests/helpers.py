"""시험에서 함께 쓰는 입력 생성기. 정상 입력 하나를 만들고 시험마다 한 군데만 망가뜨린다."""

import copy
import hashlib
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
TAILNET = "tailscale-system"
TCP_5432 = [{"protocol": "TCP", "port": 5432}]


def t16_digest(doc: dict) -> str:
    """T16 report.py의 digest와 같은 계산. 고정 입력을 고친 뒤 다시 맞출 때 쓴다."""
    core = {k: v for k, v in doc.items() if k not in {"generated_at", "digest"}}
    raw = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def inventory_doc() -> dict:
    return json.loads((FIXTURES / "inventory-demo-chat.json").read_text(encoding="utf-8"))


def redigest(doc: dict) -> dict:
    doc["digest"] = t16_digest(doc)
    return doc


def spec_doc() -> dict:
    return copy.deepcopy(_SPEC)


_SPEC = {
    "schema_version": "exit-drill-spec/v1",
    "namespace": "target-dev",
    "aws_region": "ap-northeast-2",
    "workloads": {
        "app": {"selector": {"app": "demo-chat"}, "role": "app", "aws_client": True},
        "pooler": {"selector": {"app": "target-pgbouncer"}, "role": "app"},
        "db": {"selector": {"cnpg.io/cluster": "target-db"}, "role": "replication"},
        "objects": {"selector": {"app.kubernetes.io/name": "target-seaweedfs"}, "role": "infrastructure"},
        "control": {"selector": {"bravo-exit-drill-control": "true"}, "role": "control"},
    },
    "routes": [
        {
            "id": "dns",
            "class": "local",
            "owners": ["app", "pooler"],
            "to": {"namespace": "kube-system", "selector": {"k8s-app": "kube-dns"}},
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
            "probe": {"kind": "dns", "host": "kubernetes.default.svc.cluster.local"},
        },
        {
            "id": "app-to-pooler",
            "class": "local",
            "owners": ["app"],
            "to": {"selector": {"app": "target-pgbouncer"}},
            "ports": TCP_5432,
            "probe": {"kind": "tcp", "host": "target-pgbouncer.target-dev.svc", "port": 5432},
        },
        {
            "id": "app-to-objects",
            "class": "local",
            "owners": ["app"],
            "to": {"selector": {"app.kubernetes.io/name": "target-seaweedfs"}},
            "ports": [{"protocol": "TCP", "port": 8333}],
            "probe": {"kind": "tcp", "host": "seaweedfs.target-dev.svc", "port": 8333},
        },
        {
            "id": "pooler-to-db",
            "class": "local",
            "owners": ["pooler"],
            "to": {"selector": {"cnpg.io/cluster": "target-db", "cnpg.io/instanceRole": "primary"}},
            "ports": TCP_5432,
            "probe": {"kind": "tcp", "host": "target-db-rw.target-dev.svc", "port": 5432},
        },
        {
            "id": "pooler-to-source",
            "class": "source_access",
            "owners": ["pooler"],
            "to": {"namespace": TAILNET, "selector": {"tailscale.com/parent-resource": "bravo-target-pooler"}},
            "ports": TCP_5432,
            "probe": {"kind": "tcp", "host": "source-for-pooler.target-dev.svc", "port": 5432},
            "roles": ["db"],
        },
        {
            "id": "replication-forward",
            "class": "replication",
            "owners": ["db"],
            "to": {"namespace": TAILNET, "selector": {"tailscale.com/parent-resource": "bravo-target-replication"}},
            "ports": TCP_5432,
            "evidence": "tcp",
            "probe": {"kind": "tcp", "host": "source-db-for-replication.target-dev.svc", "port": 5432},
        },
        {
            "id": "replication-reverse",
            "class": "replication",
            "owners": ["db"],
            "to": {"namespace": TAILNET, "selector": {"tailscale.com/parent-resource": "bravo-target-db"}},
            "ports": TCP_5432,
            "evidence": "stream",
        },
    ],
    "new_connection_checks": [
        {"id": "http-ready", "kind": "http", "host": "demo-chat.target-dev.svc", "port": 8080, "path": "/readyz"},
        {"id": "db-via-pooler", "kind": "http", "host": "demo-chat.target-dev.svc", "port": 8080, "path": "/api/health/db"},
        {"id": "websocket", "kind": "websocket", "host": "demo-chat.target-dev.svc", "port": 8080, "path": "/ws"},
    ],
    "declared_residuals": ["tailscale (복제 · 제어 경로)"],
}


def pod(name: str, labels: dict, *, host_network: bool = False, phase: str = "Running", namespace: str = "target-dev") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}", "labels": labels},
        "spec": {"hostNetwork": host_network} if host_network else {},
        "status": {"phase": phase},
    }


def network_policy(name: str, selector: dict, egress=None, *, types=("Egress",), namespace: str = "target-dev") -> dict:
    spec: dict = {"podSelector": selector, "policyTypes": list(types)}
    if egress is not None:
        spec["egress"] = egress
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def healthy_pods() -> list[dict]:
    return [
        pod("demo-chat-1", {"app": "demo-chat"}),
        pod("pgbouncer-1", {"app": "target-pgbouncer"}),
        pod("target-db-1", {"cnpg.io/cluster": "target-db", "cnpg.io/instanceRole": "primary"}),
        pod("seaweedfs-1", {"app.kubernetes.io/name": "target-seaweedfs"}),
        pod("control-1", {"bravo-exit-drill-control": "true"}),
    ]


def snapshot_doc(pods=None, policies=()) -> dict:
    items = list(healthy_pods() if pods is None else pods) + list(policies)
    return {"apiVersion": "v1", "kind": "List", "items": items}
