"""명세가 실제 목적지 렌더러와 맞는지 대조한다. 렌더러를 실행하지만 클러스터에는 접속하지 않는다.

CNPG Pod는 operator가 만들어 렌더 결과에 없으므로, CNPG가 붙이는 레이블로 만든다. 앱 렌더러는 #210으로
들어온다. 없으면 그 시험만 건너뛰고, 변이 시험 사본에는 저장소 렌더러가 없어 모두 건너뛴다.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import inventory_doc, spec_doc
from test_verdict import good_evidence

from bravo_exit_drill import verdict as verdict_module
from bravo_exit_drill.cli import main
from bravo_exit_drill.cluster import parse_snapshot, path_open, selector_matches
from bravo_exit_drill.control_pod import render_control_pod
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.observation import normalize_pods
from bravo_exit_drill.policy import PlanRefused, build_plan
from bravo_exit_drill.residual_map import build_map
from bravo_exit_drill.spec import parse_spec
from bravo_exit_drill.verdict import exit_code, judge

WORKLOADS = Path(__file__).resolve().parents[3] / "infra/envs/target-workloads"
APP_RENDERER = WORKLOADS / "application.py"
EDGE_RENDERER = WORKLOADS.parent / "edge-tunnels" / "render.py"
IMAGE = "ghcr.io/softbank-hackathon-finals/demo-chat@sha256:" + "a" * 64
NAMESPACE = "target-dev"

pytestmark = pytest.mark.skipif(not WORKLOADS.exists(), reason="repository renderers are not in this copy")
needs_app_renderer = pytest.mark.skipif(not APP_RENDERER.exists(), reason="application.py arrives with PR #210")
needs_eligibility = pytest.mark.skipif(not hasattr(verdict_module, "_check_eligibility"), reason="eligibility arrives with PR #210")


def render(script: str, *args: str) -> list[dict]:
    done = subprocess.run([sys.executable, str(WORKLOADS / script), *args], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)["items"]


def pooler_items() -> list[dict]:
    return render("pooler.py", "--egress-address", "10.0.0.10", "--egress-service-uid", "00000000-0000-4000-8000-000000000000")


def network_items() -> list[dict]:
    # The routes are declared in infra/iac (T44) and its tests pin them equal to these functions; the
    # old `network.py ingress|egress` CLI exits 2, so call the functions in a separate interpreter.
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import network; "
            "print(json.dumps([*network.egress('target', 'bravo-source-db.example.ts.net'), network.ingress('target')]))")
    done = subprocess.run([sys.executable, "-c", code, str(WORKLOADS)], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def deployment_pod(items: list[dict], name: str) -> dict:
    deployment = next(i for i in items if i["kind"] == "Deployment" and i["metadata"]["name"] == name)
    template = deployment["spec"]["template"]
    metadata = {**template["metadata"], "name": f"{name}-1", "uid": f"uid-{name}", "namespace": NAMESPACE}
    return {"kind": "Pod", "metadata": metadata, "spec": template["spec"], "status": {"phase": "Running"}}


def cnpg_pod() -> dict:
    cluster = next(i for i in render("render.py", "database", "--node-name", "node") if i["kind"] == "Cluster")
    name = cluster["metadata"]["name"]
    labels = {"cnpg.io/cluster": name, "cnpg.io/instanceRole": "primary"}
    metadata = {"name": f"{name}-1", "uid": "uid-db", "namespace": NAMESPACE, "labels": labels}
    return {"kind": "Pod", "metadata": metadata, "spec": {"containers": [{"name": "postgres"}]}, "status": {"phase": "Running"}}


def app_pod() -> dict:
    if APP_RENDERER.exists():
        return deployment_pod(render("application.py"), "demo-chat")
    metadata = {"name": "demo-chat-1", "uid": "uid-demo-chat", "namespace": NAMESPACE, "labels": {"app": "demo-chat"}}
    return {"kind": "Pod", "metadata": metadata, "spec": {"containers": [{"name": "app"}]}, "status": {"phase": "Running"}}


def edge_deployment() -> dict:
    """T17의 Cloudflare Tunnel connector. 렌더러가 목록이 아니라 Deployment 하나를 낸다."""
    done = subprocess.run([sys.executable, str(EDGE_RENDERER), NAMESPACE], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def edge_pod() -> dict:
    return deployment_pod([edge_deployment()], "bravo-edge-tunnel")


def rendered_snapshot() -> dict:
    objects = render("objects.py", "--node-name", "node")
    control = render_control_pod(parse_spec(spec_doc()), IMAGE)
    control["status"] = {"phase": "Running"}
    policies = [i for i in [*objects, *pooler_items()] if i["kind"] == "NetworkPolicy"]
    for policy in policies:
        policy["metadata"]["namespace"] = NAMESPACE
    pods = [
        app_pod(), deployment_pod(pooler_items(), "target-pgbouncer"), deployment_pod(objects, "target-seaweedfs"),
        cnpg_pod(), edge_pod(), control,
    ]
    return {"kind": "List", "items": [*pods, *policies]}


def real_plan() -> dict:
    return build_plan(
        parse_spec(spec_doc()),
        parse_inventory(inventory_doc(), candidate="onprem-demo"),
        parse_snapshot(rendered_snapshot(), namespace=NAMESPACE),
    )


def test_real_spec_plans_against_the_rendered_target():
    """오타 난 선택자 · 없는 컨테이너 · 합집합 허용이 있으면 여기서 거부된다."""
    try:
        plan = real_plan()
    except PlanRefused as refused:
        pytest.fail(f"real target refused: {refused}")
    assert {m["metadata"]["name"] for m in plan["manifests"]} == {"exit-drill-egress-app", "exit-drill-egress-pooler"}


def test_policies_never_select_the_database_or_the_proxies():
    """복제를 끊지 않는 근거. 실제 DB Pod 레이블에 대고 정책의 선택자를 잰다."""
    db_labels = cnpg_pod()["metadata"]["labels"]
    for manifest in real_plan()["manifests"]:
        assert not selector_matches(manifest["spec"]["podSelector"], db_labels)
        assert manifest["spec"]["policyTypes"] == ["Egress"]


def test_edge_selector_is_the_rendered_deployment_selector():
    """명세가 터널을 고르는 레이블이 Deployment가 자기 Pod를 고르는 레이블과 같다."""
    assert spec_doc()["workloads"]["edge"]["selector"] == edge_deployment()["spec"]["selector"]["matchLabels"]


def test_policies_never_select_the_edge_tunnel_and_keep_its_way_to_the_app():
    """막으면 Cloudflare에서 오는 트래픽이 앱까지 못 온다. 기존 정책과 차단 정책을 함께 놓고 잰다."""
    plan = real_plan()
    tunnel, app = edge_pod()["metadata"]["labels"], app_pod()["metadata"]["labels"]
    policies = [*parse_snapshot(rendered_snapshot(), namespace=NAMESPACE).policies, *plan["manifests"]]
    for manifest in plan["manifests"]:
        assert not selector_matches(manifest["spec"]["podSelector"], tunnel)
    # 터널은 demo-chat:8080으로 넘긴다(infra/envs/edge-tunnels). 보내는 쪽과 받는 쪽을 모두 본다.
    assert path_open(policies, tunnel, NAMESPACE, NAMESPACE, app, "egress", [("TCP", 8080)])
    assert path_open(policies, app, NAMESPACE, NAMESPACE, tunnel, "ingress", [("TCP", 8080)])


def service_ports() -> dict[str, set[int]]:
    items = [*pooler_items(), *render("objects.py", "--node-name", "node"), *network_items()]
    if APP_RENDERER.exists():
        items += render("application.py")
    found = {i["metadata"]["name"]: {p["port"] for p in i["spec"]["ports"]} for i in items if i["kind"] == "Service"}
    # CNPG가 Cluster 이름으로 만드는 읽기 · 쓰기 Service.
    return {**found, "target-db-rw": {5432}}


def probed_addresses() -> list[tuple[str, int]]:
    spec = spec_doc()
    addresses = [(r["probe"]["host"], r["probe"]["port"]) for r in spec["routes"] if r.get("probe", {}).get("kind") == "tcp"]
    return addresses + [(c["host"], c["port"]) for c in spec["new_connection_checks"]]


def test_every_probe_address_is_a_rendered_service():
    services = service_ports()
    for host, port in probed_addresses():
        name, namespace = host.removesuffix(".svc").split(".")
        assert namespace == NAMESPACE
        if name == "demo-chat" and not APP_RENDERER.exists():
            continue
        assert port in services[name], f"{host}:{port}"


@needs_app_renderer
def test_new_connection_checks_use_the_rendered_app_service():
    assert service_ports()["demo-chat"] == {8080}
    assert {c["port"] for c in spec_doc()["new_connection_checks"]} == {8080}


def test_tailnet_routes_name_the_rendered_proxy_groups():
    groups = {i["metadata"]["name"] for i in network_items() if i["kind"] == "ProxyGroup"}
    named = {r["to"]["selector"]["tailscale.com/parent-resource"] for r in spec_doc()["routes"] if r["to"].get("namespace") == "tailscale-system"}
    # 역방향은 ProxyGroup이 아니라 expose Service의 프록시다. 이름만 대조하고 레이블은 실제 환경에서 확인한다.
    exposed = {i["metadata"]["name"] for i in network_items() if i["kind"] == "Service" and "tailscale.com/expose" in i["metadata"].get("annotations", {})}
    assert named == groups | exposed


def test_named_containers_are_the_rendered_ones():
    workloads = spec_doc()["workloads"]
    assert workloads["pooler"]["container"] in {c["name"] for c in deployment_pod(pooler_items(), "target-pgbouncer")["spec"]["containers"]}
    assert workloads["app"]["container"] in {c["name"] for c in app_pod()["spec"]["containers"]}


def compat_kube_pod() -> dict:
    """렌더된 호환 구성 Pod에 kubelet이 채울 상태를 붙인다. 앱 컨테이너는 정상으로 시킨 재시작을 마친 모습이다."""
    pod = deployment_pod(render("application.py"), "demo-chat")
    pod["status"] = {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": [{
            "name": "app", "restartCount": 1, "containerID": "containerd://app-2",
            "state": {"running": {}}, "lastState": {"terminated": {"containerID": "containerd://app-1"}},
        }],
        "initContainerStatuses": [{"name": "secrets-manager-compat", "restartCount": 0, "containerID": "containerd://side-1", "state": {"running": {}}}],
    }
    return pod


def compat_evidence(plan: dict) -> tuple[dict, dict]:
    """다른 증거는 모두 통과인 상태에서 앱 Pod만 호환 구성으로 바꾼다. 막아야 하는 것은 이 한 가지다."""
    before, after = good_evidence(plan)
    for observation in (before, after):
        row = normalize_pods({"items": [compat_kube_pod()]}, plan)["app"][0]
        kept = observation["pods"]["app"][0]
        observation["pods"]["app"][0] = {
            **kept, "exit_blockers": row["exit_blockers"], "container_name": row["container_name"],
        }
        for other in ("pooler", "db", "objects", "control"):
            for pod in observation["pods"][other]:
                pod["exit_blockers"] = []
    return before, after


@needs_app_renderer
@needs_eligibility
def test_compat_pod_ends_ineligible_and_still_gets_a_map(tmp_path, capsys):
    """호환 구성은 AWS를 막아도 멀쩡해 보인다. PASS가 아니라 비자격이어야 하고 지도는 그대로 나와야 한다."""
    plan = real_plan()
    before, after = compat_evidence(plan)
    report = judge(plan, before, after)
    assert report["verdict"] == "UNKNOWN"
    assert exit_code(report) == 3
    assert next(c for c in report["checks"] if c["id"] == "eligibility:app")["code"] == "NOT_EXIT_EVIDENCE"

    result = build_map(parse_inventory(inventory_doc(), candidate="onprem-demo"), plan, report)
    assert result["drill_verdict"] == "UNKNOWN"
    assert "blocked_app_ok" not in result["summary"]["status"]
    assert {"id": "eligibility:app", "verdict": "UNKNOWN", "code": "NOT_EXIT_EVIDENCE"} in result["drill_not_pass"]

    paths = {name: tmp_path / f"{name}.json" for name in ("plan", "before", "after", "inventory", "report")}
    for name, value in (("plan", plan), ("before", before), ("after", after), ("inventory", inventory_doc())):
        paths[name].write_text(json.dumps(value), encoding="utf-8")
    assert main(["check", "--plan", str(paths["plan"]), "--before", str(paths["before"]), "--after", str(paths["after"])]) == 3
    paths["report"].write_text(capsys.readouterr().out, encoding="utf-8")
    assert main([
        "map", "--inventory", str(paths["inventory"]), "--candidate", "onprem-demo", "--plan", str(paths["plan"]),
        "--report", str(paths["report"]), "--format", "markdown",
    ]) == 0
    text = capsys.readouterr().out
    assert "Exit Drill 판정: **UNKNOWN**" in text
    assert "eligibility:app" in text and "NOT_EXIT_EVIDENCE" in text


@needs_app_renderer
def test_rendered_compat_pod_still_plans():
    """호환 구성이어도 계획과 지도는 만들 수 있어야 한다. 비자격은 판정에서 말한다."""
    assert real_plan()["workloads"]["app"]["container"] == "app"
