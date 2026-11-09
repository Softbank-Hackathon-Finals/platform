"""T44 generated per-environment charts and the customer-cluster Argo CD chart that pulls them."""

import json
import re
from pathlib import Path

import pytest

from bravo_iac.chart import files, manifests
from bravo_iac.cli import main

REPO = Path(__file__).resolve().parents[3]
ENVIRONMENTS = {p.stem: json.loads(p.read_text()) for p in sorted((REPO / "infra/iac/environments").glob("*.json"))}
ARGO = REPO / "infra/iac/argocd"


@pytest.mark.parametrize("environment", sorted(ENVIRONMENTS))
def test_committed_charts_are_the_generation_of_the_committed_states(environment):
    for name, content in files(ENVIRONMENTS[environment]).items():
        assert (REPO / "infra/iac/charts" / environment / name).read_text() == content, name


def test_charts_keep_the_tailnet_and_credentials_out_of_git():
    for state in ENVIRONMENTS.values():
        template = files(state)["templates/resources.yaml"]
        # The tailnet comes from an install value; an environment without resources (an operator tag only)
        # still requires it, so it never falls back to a name written in Git.
        assert not re.search(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", template) and ".Values.tailnet }}" in template
        assert "secretKey" not in template and "Secret" not in json.dumps(manifests(state)[0])
    objects, unmanaged = manifests(ENVIRONMENTS["target-dev"])
    # Credentials go to their own applier; access bindings to a cluster administrator (operator-access).
    assert unmanaged == ["cluster-audit:NodeFile/-/audit-policy",
                         "operator-access:ClusterRoleBinding/-/bravo-operators-cluster-admin",
                         "target-object-exposure:Secret/target-dev/target-seaweedfs-auth"]
    assert {o["kind"] for o in objects} == {"ProxyGroup", "Service", "NetworkPolicy"}


def test_a_group_on_another_tailnet_needs_its_own_install_value():
    platform = files(ENVIRONMENTS["platform"])
    schema = json.loads(platform["values.schema.json"])
    assert schema["required"] == ["tailnet", "tailnets"] and schema["properties"]["tailnets"]["required"] == ["workload"]
    assert 'index .Values.tailnets "workload"' in platform["templates/resources.yaml"]
    assert 'required "tailnets.workload' in platform["templates/resources.yaml"]
    for environment in ("source-dev", "target-dev"):
        assert json.loads(files(ENVIRONMENTS[environment])["values.schema.json"])["required"] == ["tailnet"]


def test_objects_are_complete_kubernetes_manifests():
    objects, _ = manifests(ENVIRONMENTS["platform"])
    by_key = {(o["kind"], o["metadata"].get("namespace"), o["metadata"]["name"]): o for o in objects}
    group = by_key[("ProxyGroup", None, "bravo-cutover-control")]
    assert group["apiVersion"] == "tailscale.com/v1alpha1" and "namespace" not in group["metadata"]
    service = by_key[("Service", "bravo-platform", "source-db-for-cutover")]
    assert service["metadata"]["annotations"]["tailscale.com/proxy-group"] == "bravo-cutover-control"
    assert service["spec"]["type"] == "ExternalName" and "annotations" not in service["spec"]
    policy = by_key[("NetworkPolicy", "tailscale-system", "bravo-cutover-control-ingress")]
    assert policy["apiVersion"] == "networking.k8s.io/v1" and policy["spec"]["policyTypes"] == ["Ingress"]
    assert all(o["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "bravo-iac" for o in objects)
    # Namespaces belong to whoever builds the cluster (T45 for a new destination): never declared here.
    assert not [o for o in objects if o["kind"] == "Namespace"]
    for environment in ENVIRONMENTS:
        assert "kind\": \"Namespace" not in files(ENVIRONMENTS[environment])["templates/resources.yaml"]


def test_the_argo_chart_accepts_exactly_the_committed_environments():
    schema = json.loads((ARGO / "values.schema.json").read_text())
    # Customer clusters only: the platform cluster attaches charts/platform to its existing T43 Argo.
    assert sorted(schema["properties"]["environment"]["enum"]) == sorted(set(ENVIRONMENTS) - {"platform"})
    # A merge alone never reaches a customer cluster: the reviewed commit is a required install value.
    assert schema["properties"]["targetRevision"]["pattern"] == "^[0-9a-f]{40}$"
    assert 'targetRevision: ""' in (ARGO / "values.yaml").read_text()
    application = (ARGO / "templates/application.yaml").read_text()
    assert "path: infra/iac/charts/{{ $environment }}" in application and "tailnet: {{ $tailnet | quote }}" in application
    assert "targetRevision: {{ $revision | quote }}" in application
    # Namespaces are the cluster builder's; the Application is enabled after them, so retries are finite
    # (an unlimited retry keeps the failed operation running and a fixed revision never syncs).
    assert "CreateNamespace=true" not in application and "limit: 5" in application and "limit: -1" not in application
    values = (ARGO / "values.yaml").read_text()
    assert 'tailnet: ""' in values and "server:\n    enabled: false" in values


def test_cli_writes_the_chart(tmp_path, capsys):
    state = REPO / "infra/iac/environments/source-dev.json"
    assert main(["chart", str(state), "--out", str(tmp_path / "chart")]) == 0
    assert sorted(json.loads(capsys.readouterr().out)["files"]) == sorted(files(ENVIRONMENTS["source-dev"]))
    assert (tmp_path / "chart/templates/resources.yaml").read_text() == files(ENVIRONMENTS["source-dev"])["templates/resources.yaml"]


def network_values(side):
    import importlib.util
    spec = importlib.util.spec_from_file_location("t8_network", REPO / "infra/envs/target-workloads/network.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded, loaded.values(side)


def test_the_committed_operator_is_the_render_of_the_pinned_chart_with_network_values():
    # T48: the source operator is taken over by the customer Argo CD as it runs, except the API proxy mode.
    network, values = network_values("source")
    text = (REPO / "infra/iac/operators/source-dev/operator.yaml").read_text()
    assert text.startswith("# Generated by infra/iac/operators/render.py source-dev:")
    assert f"sha256 {network.CHART_SHA256}" in text.splitlines()[0]
    env = dict(re.findall(r"- name: (\w+)\n\s+value: \"?([^\"\n]+)\"?", text))
    assert env["APISERVER_PROXY"] == values["apiServerProxyConfig"]["mode"] == "noauth"
    assert env["PROXY_IMAGE"] == "tailscale/tailscale@" + values["proxyConfig"]["image"]["digest"]
    assert env["OPERATOR_HOSTNAME"] == "bravo-source-operator" and env["PROXY_TAGS"] == "tag:bravo-source-db"
    assert "image: tailscale/k8s-operator@" + values["operatorConfig"]["image"]["digest"] in text
    kinds = re.findall(r"^kind: (\S+)", text, re.M)
    assert "Secret" not in kinds  # operator-oauth belongs to whoever built the cluster, never to Git


def test_the_operator_project_allows_exactly_what_the_operator_files_hold():
    held = set()
    for name in ("operator.yaml", "access.yaml"):
        text = (REPO / "infra/iac/operators/source-dev" / name).read_text()
        for api, kind in re.findall(r'^\s*"?apiVersion"?: "?([^"\n,]+)"?,?\n\s*"?kind"?: "?([^"\n,]+)"?', text, re.M):
            held.add((api.rsplit("/", 1)[0] if "/" in api else "", kind))
    template = (ARGO / "templates/operator.yaml").read_text()
    allowed = set(re.findall(r"- group: \"?([^\"\n]*)\"?\n\s+kind: (\S+)", template))
    assert allowed == held
    assert "range .Values.operator.namespaces" in template and "prune: false" in template
    values = (ARGO / "values.yaml").read_text()
    assert "operator:\n  enabled: false\n  autoSync: false" in values  # never on by default
    assert "namespaces: [tailscale-system]" in values


def test_workload_api_access_renders_one_group_per_tag_and_its_proxy():
    # T48: platform workloads reach source through a separate auth proxy; each tag gets its own group and Role.
    import json as _json
    access = (REPO / "infra/iac/operators/source-dev/access.yaml").read_text().split("---\n")[1:]
    objects = [_json.loads(o) for o in access]
    [group] = [o for o in objects if o["kind"] == "ProxyGroup"]
    assert group["spec"]["type"] == "kube-apiserver" and group["spec"]["kubeAPIServer"] == {
        "mode": "auth", "hostname": "bravo-source-api"} and group["spec"]["tags"] == ["tag:bravo-source-api"]
    bindings = {(o["metadata"]["namespace"], o["subjects"][0]["name"]) for o in objects if o["kind"] == "RoleBinding"}
    assert bindings == {("source-dev", "bravo:cutover-executor"), ("source-gce", "bravo:cutover-executor-gce")}
    network, values = network_values("source")
    assert values["apiServerProxyConfig"] == {"mode": "noauth", "allowImpersonation": "true"}
    assert "name: kube-apiserver-auth-proxy" in (REPO / "infra/iac/operators/source-dev/operator.yaml").read_text()
