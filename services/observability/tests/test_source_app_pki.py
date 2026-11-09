"""출발지 앱 지표 mTLS의 앱 CA: cert-manager가 클러스터 안에서 만들고, 앱 · Collector 설정과 이름 · 용도가 맞는다(#104 할 일 7)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "services/observability/deploy"
PKI = DEPLOY / "app-pki-source"
FILES = ("selfsigned-issuer.json", "app-ca.json", "app-ca-issuer.json", "collector-server.json", "app-client.json")


def load(name):
    return json.loads((PKI / name).read_text())


def test_the_ca_key_is_made_in_the_cluster_and_kept_by_one_issuer():
    bootstrap, ca, ca_issuer = load("selfsigned-issuer.json"), load("app-ca.json"), load("app-ca-issuer.json")
    assert (bootstrap["kind"], bootstrap["spec"]) == ("ClusterIssuer", {"selfSigned": {}})
    spec = ca["spec"]
    assert ca["kind"] == "Certificate" and spec["isCA"] is True
    # cert-manager의 클러스터 자원 namespace: ClusterIssuer가 CA Secret을 여기서만 읽는다
    assert ca["metadata"]["namespace"] == "cert-manager"
    assert spec["issuerRef"] == {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": bootstrap["metadata"]["name"]}
    assert set(spec["usages"]) == {"cert sign", "crl sign"}
    # 갱신해도 키가 같아야 이미 발급된 서버 · 클라이언트 인증서가 계속 검증된다
    assert spec["privateKey"]["rotationPolicy"] == "Never"
    assert ca_issuer["kind"] == "ClusterIssuer" and ca_issuer["spec"] == {"ca": {"secretName": spec["secretName"]}}


def test_leaves_come_from_the_app_ca_with_one_purpose_each():
    ca_issuer = load("app-ca-issuer.json")["metadata"]["name"]
    server, client = load("collector-server.json")["spec"], load("app-client.json")["spec"]
    for leaf in (server, client):
        assert leaf["issuerRef"] == {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": ca_issuer}
        assert "isCA" not in leaf
        assert leaf["privateKey"]["rotationPolicy"] == "Always"
    assert set(server["usages"]) == {"server auth", "digital signature"}
    assert set(client["usages"]) == {"client auth", "digital signature"}


def test_names_match_what_the_app_and_collector_read():
    server, client = load("collector-server.json"), load("app-client.json")
    app_mtls = (DEPLOY / "components/app-mtls/kustomization.yaml").read_text()
    assert server["metadata"]["namespace"] == "bravo-observability"
    assert f"secretName: {server['spec']['secretName']}" in app_mtls
    # 앱이 부르는 주소가 서버 SAN에 있어야 앱이 Collector를 믿는다
    render = (ROOT / "infra/envs/source-workloads/render.py").read_text()
    host = re.search(r'"BRAVO_METRICS_ENDPOINT": "https://([^:/"]+)', render).group(1)
    assert host in server["spec"]["dnsNames"]
    namespace = re.search(r'^NAMESPACE = "([^"]+)"', render, re.M).group(1)
    assert client["metadata"]["namespace"] == namespace
    assert f'"secretName": "{client["spec"]["secretName"]}"' in render
    # 앱은 ca.crt · tls.crt · tls.key를 읽는다: cert-manager Secret의 키 이름과 같다
    for key in ("ca.crt", "tls.crt", "tls.key"):
        assert f"/etc/bravo-metrics/{key}" in render


def test_the_collector_trusts_the_issuing_ca_as_client_ca():
    receiver = (DEPLOY / "components/app-mtls/receiver-mtls.yaml").read_text()
    assert receiver.count("client_ca_file: /etc/bravo-app-tls/client-ca.crt") == 2  # HTTP · gRPC
    component = (DEPLOY / "components/app-mtls-cert-manager/kustomization.yaml").read_text()
    assert "- {key: ca.crt, path: client-ca.crt}" in component
    assert "secretName: bravo-collector-app-tls" in component
    source = (DEPLOY / "collector-source/kustomization.yaml").read_text()
    assert source.index("../components/app-mtls\n") < source.index("../components/app-mtls-cert-manager")


def test_the_bundle_ships_exactly_these_resources_and_no_key_material():
    kustomization = (PKI / "kustomization.yaml").read_text()
    assert re.findall(r"^  - (\S+)$", kustomization, re.M) == list(FILES)
    for path in [PKI / f for f in FILES] + [DEPLOY / "components/app-mtls-cert-manager/kustomization.yaml"]:
        assert "PRIVATE KEY" not in path.read_text()


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl kustomize not installed")
def test_kustomize_renders_the_mapping_and_the_pki():
    def render(path):
        return subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True, check=True).stdout

    collector = render(DEPLOY / "collector-source")
    assert collector.count("name: app-tls\n") == 2  # 한 볼륨과 그 마운트
    assert re.search(r"- key: ca\.crt\n\s+path: client-ca\.crt", collector)
    pki = render(PKI)
    assert re.findall(r"^kind: (\S+)$", pki, re.M).count("Certificate") == 3
    assert re.findall(r"^kind: (\S+)$", pki, re.M).count("ClusterIssuer") == 2
