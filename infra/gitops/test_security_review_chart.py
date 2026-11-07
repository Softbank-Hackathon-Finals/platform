import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/charts/security-review"
DIGEST = "sha256:" + "a" * 64
REVISION = "b" * 40


def render():
    result = subprocess.run([
        "helm", "template", "security-review", str(CHART), "--namespace", "platform-state",
        "--set", f"image.digest={DIGEST}", "--set", f"sourceRevision={REVISION}",
        "--set", "tenantId=bravo", "--set", "tokenReview.apiAddress=10.43.0.1",
        "--set", "database.address=10.43.10.10", "--set", "secrets.tls=security-review-tls",
        "--set", "secrets.database=security-review-database",
        "--set", "configMaps.grants=security-review-grants",
        "--set", "configMaps.apiCA=security-review-api-ca",
    ], check=True, capture_output=True, text=True)
    return list(yaml.safe_load_all(result.stdout))


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
    assert token == {"path": "token", "audience": "registered-security-review-audience", "expirationSeconds": 600}
    assert sources[1]["configMap"]["name"] == "security-review-api-ca"


def test_review_service_and_network_are_private_and_explicit():
    documents = render()
    service = next(item for item in documents if item["kind"] == "Service")
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    policy = next(item for item in documents if item["kind"] == "NetworkPolicy")
    egress = policy["spec"]["egress"]
    assert egress[1]["to"][0]["ipBlock"]["cidr"] == "10.43.0.1/32"
    assert egress[2]["to"][0]["ipBlock"]["cidr"] == "10.43.10.10/32"
    assert policy["spec"]["policyTypes"] == ["Egress"]
