"""시험에서 함께 쓰는 입력 생성기. 정상 입력 하나를 만들고 시험마다 한 군데만 망가뜨린다."""

import hashlib
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
PROFILE = Path(__file__).parents[1] / "profiles" / "target-dev.json"
TAILNET = "tailscale-system"
# app 레이블별 컨테이너 이름. 명세의 workloads.*.container와 같다.
CONTAINERS = {"demo-chat": "app", "target-pgbouncer": "target-pgbouncer"}


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
    """저장소에 있는 실제 명세를 쓴다. 시험이 곧 이 명세의 시험이다."""
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def pod(name: str, labels: dict, *, host_network: bool = False, phase: str = "Running", namespace: str = "target-dev", containers=None) -> dict:
    names = [CONTAINERS.get(labels.get("app"), "main")] if containers is None else containers
    spec: dict = {"containers": [{"name": c} for c in names]}
    if host_network:
        spec["hostNetwork"] = True
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}", "labels": labels},
        "spec": spec,
        "status": {"phase": phase},
    }


def network_policy(name: str, selector: dict, egress=None, *, types=("Egress",), ingress=None, namespace: str = "target-dev") -> dict:
    spec: dict = {"podSelector": selector, "policyTypes": list(types)}
    if egress is not None:
        spec["egress"] = egress
    if ingress is not None:
        spec["ingress"] = ingress
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
