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
        assert not re.search(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", template) and "{{ .Values.tailnet }}" in template
        assert "secretKey" not in template and "Secret" not in json.dumps(manifests(state)[0])
    objects, unmanaged = manifests(ENVIRONMENTS["target-dev"])
    # Credentials go to their own applier; access bindings to a cluster administrator (operator-access).
    assert unmanaged == ["operator-access:ClusterRoleBinding/-/bravo-operators-cluster-admin",
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
    # Namespaces are T45's; on an empty cluster the Tailscale CRDs come after this Application.
    assert "CreateNamespace=true" not in application and "limit: -1" in application
    values = (ARGO / "values.yaml").read_text()
    assert 'tailnet: ""' in values and "server:\n    enabled: false" in values


def test_cli_writes_the_chart(tmp_path, capsys):
    state = REPO / "infra/iac/environments/source-dev.json"
    assert main(["chart", str(state), "--out", str(tmp_path / "chart")]) == 0
    assert sorted(json.loads(capsys.readouterr().out)["files"]) == sorted(files(ENVIRONMENTS["source-dev"]))
    assert (tmp_path / "chart/templates/resources.yaml").read_text() == files(ENVIRONMENTS["source-dev"])["templates/resources.yaml"]
