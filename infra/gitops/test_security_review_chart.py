import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/charts/security-review"
GITOPS_CHART = ROOT / "infra/gitops/argocd"
DIGEST = "sha256:" + "a" * 64
REVISION = "b" * 40


def render():
    result = subprocess.run([
        "helm", "template", "security-review", str(CHART), "--namespace", "platform-state",
        "--set", f"image.digest={DIGEST}", "--set", f"sourceRevision={REVISION}",
        "--set", "tenantId=bravo", "--set", "tokenReview.apiAddress=10.81.0.191",
        "--set", "tokenReview.apiPort=6443",
        "--set", "tokenReview.reviewerAudience=https://kubernetes.default.svc.cluster.local",
        "--set", "database.address=10.43.10.10", "--set", "secrets.tls=security-review-tls",
        "--set", "secrets.database=security-review-database",
        "--set", "configMaps.grants=security-review-grants",
        "--set", "configMaps.apiCA=security-review-api-ca",
    ], check=True, capture_output=True, text=True)
    return list(yaml.safe_load_all(result.stdout))


def render_gitops():
    result = subprocess.run([
        "helm", "template", "bravo-gitops", str(GITOPS_CHART), "--namespace", "bravo-gitops",
        "--set", "publicEntry.enabled=true", "--set", "observability.enabled=true",
        "--set", "securityReview.enabled=true",
    ], check=True, capture_output=True, text=True)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def test_review_workload_has_only_projected_review_identity_and_read_only_runtime():
    documents = render()
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "security-review"
    container, = pod["containers"]
    assert container["image"] == "ghcr.io/softbank-hackathon-finals/platform-security-review@" + DIGEST
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    volumes = {item["name"]: item for item in pod["volumes"]}
    sources = volumes["api"]["projected"]["sources"]
    token = sources[0]["serviceAccountToken"]
    assert token == {
        "path": "token",
        "audience": "https://kubernetes.default.svc.cluster.local",
        "expirationSeconds": 600,
    }
    assert token["audience"] != "registered-security-review-audience"
    assert sources[1]["configMap"]["name"] == "security-review-api-ca"


def test_review_service_and_network_are_private_and_explicit():
    documents = render()
    service = next(item for item in documents if item["kind"] == "Service")
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    policy = next(item for item in documents if item["kind"] == "NetworkPolicy")
    egress = policy["spec"]["egress"]
    assert egress[1]["to"][0]["ipBlock"]["cidr"] == "10.81.0.191/32"
    assert egress[1]["ports"] == [{"port": 6443, "protocol": "TCP"}]
    assert egress[2]["to"][0]["ipBlock"]["cidr"] == "10.43.10.10/32"
    assert policy["spec"]["policyTypes"] == ["Egress"]


def test_gitops_admission_and_rbac_allow_only_named_review_state_objects():
    documents = render_gitops()
    policy = next(item for item in documents if item["kind"] == "ValidatingAdmissionPolicy"
                  and item["metadata"]["name"] == "platform-gitops-state-names")
    rule, = policy["spec"]["matchConstraints"]["resourceRules"]
    assert "persistentvolumeclaims" in rule["resources"]
    expression = policy["spec"]["validations"][0]["expression"]
    for name in ("security-review-config", "security-review-egress", "security-review-audit"):
        assert name in expression

    role = next(item for item in documents if item["kind"] == "Role"
                and item["metadata"]["name"] == "platform-gitops-security-review")
    create_rules = [rule for rule in role["rules"] if "create" in rule["verbs"]]
    assert any(rule["resources"] == ["persistentvolumeclaims"]
               and "resourceNames" not in rule for rule in create_rules)
