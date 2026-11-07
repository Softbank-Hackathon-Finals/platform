"""ADR-0034 source slots: per-kind static rules and deploy adapters (A3)."""

import json
from pathlib import Path

import pytest

from bravo_inventory import deploy_trace
from bravo_inventory import reconcile as reconcile_module
from bravo_inventory.cli import main
from bravo_inventory.report import build_inventory
from bravo_inventory.source_rules import (
    KINDS,
    STATIC_RULES,
    UNSUPPORTED,
    Unsupported,
    UnsupportedKind,
    static_rules_for,
)
from bravo_inventory.sources import DEPLOY_ADAPTERS, deploy_adapter_for, slot_status
from bravo_inventory.sources import aws as aws_source
from bravo_inventory.sources.gcp import GcloudAdapter, GcpFileAdapter
from bravo_inventory.sources.onprem import OnpremFileAdapter, policy_paths
from bravo_inventory.static_scan import scan_repo, use_source_kind

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "environment-kinds.json"


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _by(findings, kind):
    return [f for f in findings if f.kind == kind]


# --- registries ---------------------------------------------------------------------------------


def test_registries_cover_exactly_the_contract_kinds():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert set(KINDS) == set(contract["kinds"]) == set(STATIC_RULES) == set(DEPLOY_ADAPTERS)
    assert contract["unsupported"] == UNSUPPORTED
    for kind, spec in contract["kinds"].items():
        # every endpoint suffix of a kind is a cloud dependency under that kind's own rules
        for suffix in spec["endpoint_suffixes"]:
            assert STATIC_RULES[kind].endpoint_cloud(f"https://x.{suffix}") == kind


def test_unregistered_kind_is_unsupported_never_another_kinds_rules(tmp_path):
    assert isinstance(static_rules_for("azure"), Unsupported)
    with pytest.raises(UnsupportedKind, match=UNSUPPORTED):
        scan_repo(tmp_path, ".", "azure")
    u = deploy_adapter_for("azure", tmp_path)
    assert isinstance(u, Unsupported) and u.status == UNSUPPORTED and u.slot == "source.deploy_trace"
    live = deploy_adapter_for("onprem", tmp_path, live=True)
    assert isinstance(live, Unsupported) and "live" in live.reason
    assert slot_status("azure") == {"source.static_rules": UNSUPPORTED, "source.deploy_trace": UNSUPPORTED}
    assert slot_status("gcp") == {"source.static_rules": "ok", "source.deploy_trace": "ok"}


# --- aws: unchanged T16 behavior ----------------------------------------------------------------


def test_aws_rules_are_the_default_and_unchanged(repo):
    default = scan_repo(repo, "app")
    explicit = scan_repo(repo, "app", "aws")
    assert [f.as_dict() for f in default] == [f.as_dict() for f in explicit]
    assert {f.source_kind for f in explicit} == {"aws"}
    secret = [f for f in _by(explicit, "config_env") if f.detail["name"] == "AWS_SECRET_ACCESS_KEY"][0]
    assert secret.role == "iam" and secret.cloud_dependency is True
    s3 = [f for f in _by(explicit, "sdk_call") if f.service == "s3"][0]
    assert s3.cloud_dependency is True and "compat" not in s3.detail


def test_aws_trace_moved_into_aws_adapter_with_t16_names(exports):
    assert deploy_trace.FileAdapter is aws_source.FileAdapter
    assert deploy_trace.IRSA_ANNOTATION == aws_source.IRSA_ANNOTATION == "eks.amazonaws.com/role-arn"
    adapter = deploy_adapter_for("aws", exports)
    assert isinstance(adapter, aws_source.FileAdapter)
    g = adapter.trace("chat")
    assert g.source_kind == "aws" and g.as_dict()["source_kind"] == "aws"
    assert ("iam_role", "arn:aws:iam::000000000000:role/chat-irsa") in {(r.kind, r.name) for r in g.resources}


# --- gcp ----------------------------------------------------------------------------------------

GCP_APP = {
    "app/gcs.py": (
        "import os\n"
        "from google.cloud import storage, secretmanager\n"
        "from google.cloud import kms\n"
        "import google.cloud.logging\n"
        "import google.auth\n"
        "from google.cloud.storage import Client as GcsClient\n"
        "client = storage.Client()\n"
        "secrets = secretmanager.SecretManagerServiceClient()\n"
        "keys = kms.KeyManagementServiceClient()\n"
        "log = google.cloud.logging.Client()\n"
        "creds, project = google.auth.default()\n"
        "other = GcsClient.from_service_account_json(os.environ['GOOGLE_APPLICATION_CREDENTIALS'])\n"
        "blob = storage.Blob('x', None)\n"
        "URL = 'https://storage.googleapis.com/demo-chat-attachments'\n"
        "REGION = 'asia-northeast3'\n"
        "bucket = os.environ['GCS_BUCKET']\n"
    ),
    ".env.example": "GOOGLE_CLOUD_PROJECT=example-project\nGCS_BUCKET=demo-chat-attachments\nGCP_KMS_KEY=attachments\nGOOGLE_APPLICATION_CREDENTIALS=/var/run/key.json\n",
    "infra/main.tf": (
        'resource "google_storage_bucket" "attachments" {}\n'
        'resource "google_storage_bucket_iam_member" "chat" {}\n'
        'resource "google_secret_manager_secret" "db" {}\n'
        'resource "google_kms_crypto_key" "attachments" {}\n'
        'resource "google_service_account" "chat" {}\n'
        'resource "google_logging_project_sink" "audit" {}\n'
        'resource "kubernetes_namespace" "chat" {}\n'
    ),
}


def test_gcp_sdk_calls_are_classified_by_role(tmp_path):
    findings = scan_repo(_write(tmp_path, GCP_APP), "app", "gcp")
    calls = {(f.service, f.role) for f in _by(findings, "sdk_call")}
    assert {("storage", "object_store"), ("secretmanager", "secrets"), ("kms", "kms"), ("logging", "audit"), ("auth", "iam")} <= calls
    storage_calls = [f for f in _by(findings, "sdk_call") if f.service == "storage"]
    assert len(storage_calls) == 2  # storage.Client() and GcsClient.from_service_account_json(); not storage.Blob()
    assert not _by(findings, "other_cloud_import")  # the source's own SDK is not "other cloud"
    assert {f.source_kind for f in findings} == {"gcp"} and all(f.cloud_dependency for f in _by(findings, "sdk_call"))
    endpoint = _by(findings, "endpoint_literal")[0]
    assert endpoint.service == "gcp" and endpoint.detail["value"] == "https://storage.googleapis.com"
    assert _by(findings, "region_literal")[0].detail["value"] == "asia-northeast3"


def test_gcp_env_and_terraform_are_classified(tmp_path):
    findings = scan_repo(_write(tmp_path, GCP_APP), "app", "gcp")
    env = {f.detail["name"]: f.role for f in _by(findings, "config_env")}
    assert env == {"GOOGLE_CLOUD_PROJECT": "iam", "GCS_BUCKET": "object_store", "GCP_KMS_KEY": "kms", "GOOGLE_APPLICATION_CREDENTIALS": "iam"}
    refs = {f.detail["name"]: f.role for f in _by(findings, "env_ref")}
    assert refs == {"GOOGLE_APPLICATION_CREDENTIALS": "iam", "GCS_BUCKET": "object_store"}
    tf = {f.service: f.role for f in _by(findings, "iac_resource")}
    assert tf == {
        "google_storage_bucket": "object_store", "google_storage_bucket_iam_member": "iam", "google_secret_manager_secret": "secrets",
        "google_kms_crypto_key": "kms", "google_service_account": "iam", "google_logging_project_sink": "audit",
    }


def test_same_gcp_code_under_aws_rules_stays_other_cloud(tmp_path):
    findings = scan_repo(_write(tmp_path, GCP_APP), "app", "aws")
    assert _by(findings, "other_cloud_import") and not [f for f in _by(findings, "sdk_call") if f.service == "storage"]
    assert not _by(findings, "iac_resource")  # google_* is not an AWS resource


def test_gcp_file_adapter_follows_workload_identity(tmp_path):
    adapter = deploy_adapter_for("gcp", FIXTURES / "exports" / "gcp")
    assert isinstance(adapter, GcpFileAdapter)
    g = adapter.trace("chat")
    res = {(r.kind, r.name): r for r in g.resources}
    gsa = "chat-gsa@example-project.iam.gserviceaccount.invalid"
    key = "projects/example-project/locations/asia-northeast3/keyRings/chat/cryptoKeys/attachments"
    assert ("gcp_service_account", gsa) in res
    bucket = res[("gcs_bucket", "demo-chat-attachments")]
    assert bucket.role == "object_store" and bucket.actions == ["roles/storage.objectAdmin"]
    assert f"GSA/{gsa}" in bucket.via and bucket.attributes["referenced_by_env"] == "Deployment/chat:GCS_BUCKET"
    assert bucket.attributes["public"] == "False" and bucket.attributes["location"] == "ASIA-NORTHEAST3"
    assert "owner_email" not in bucket.attributes  # readback attributes are allowlisted
    assert res[("gcs_bucket", "demo-chat-backups")].attributes["public"] == "True"
    assert res[("gcp_secret", "demo-chat-db")].attributes["referenced_by_env"] == "Deployment/chat:SECRET_ID"
    assert res[("gcp_kms_key", key)].attributes["referenced_by_env"] == "Deployment/chat:KMS_KEY_NAME"
    assert res[("gcp_project_role", "roles/logging.logWriter")].role == "audit"
    # an annotated service account no Deployment uses (ESO) is followed too, and its missing
    # Workload Identity binding is reported
    assert any("ServiceAccount/external-secrets" in r.via and r.kind == "gcp_secret" for r in g.resources)
    assert any("eso-gsa" in n and "workloadIdentityUser" in n for n in g.notes)
    assert not any("chat-gsa" in n and "workloadIdentityUser" in n for n in g.notes)
    assert g.source_kind == "gcp" and "file:gcp.json" in g.api_calls


def test_gcloud_adapter_uses_read_only_allowlist(tmp_path):
    exports = FIXTURES / "exports" / "gcp"
    fixture = json.loads((exports / "gcp.json").read_text(encoding="utf-8"))
    seen: list[list[str]] = []

    def run(argv):
        seen.append(argv)
        assert argv[0] == "gcloud" and argv[-1] == "--format=json"
        if argv[1:3] == ["projects", "get-iam-policy"]:
            return json.dumps(fixture["project_policy"])
        if argv[1:4] == ["iam", "service-accounts", "get-iam-policy"]:
            return json.dumps(fixture["service_accounts"][argv[4]]["iam_policy"])
        if argv[1:3] == ["asset", "search-all-iam-policies"]:
            return json.dumps(fixture["resource_policies"])
        raise AssertionError(argv)

    adapter = GcloudAdapter(exports, project_id="example-project", run=run)
    g = adapter.trace("chat")
    assert ("gcs_bucket", "demo-chat-attachments") in {(r.kind, r.name) for r in g.resources}
    assert {c for c in g.api_calls if c.startswith("gcloud")} == {
        "gcloud projects get-iam-policy", "gcloud iam service-accounts get-iam-policy", "gcloud asset search-all-iam-policies"}
    assert all(a[1:3] != ["storage", "buckets"] for a in seen)
    with pytest.raises(PermissionError):
        adapter.gcloud("storage", "buckets", "delete", "gs://demo-chat-attachments")
    with pytest.raises(PermissionError):
        adapter.gcloud("projects", "set-iam-policy", "example-project", "policy.json")


# --- onprem -------------------------------------------------------------------------------------

ONPREM_APP = {
    "app/storage.py": (
        "import os\nimport boto3\nimport hvac\n"
        "s3 = boto3.client('s3', endpoint_url=os.environ['AWS_ENDPOINT_URL_S3'])\n"
        "key = os.environ['AWS_ACCESS_KEY_ID']\n"
        "vault = hvac.Client(url=os.environ['VAULT_ADDR'])\n"
    ),
    "compose.yaml": (
        "services:\n  app:\n    environment:\n"
        "      AWS_ACCESS_KEY_ID: chat\n      AWS_SECRET_ACCESS_KEY: fixture-not-a-real-value\n"
        "      AWS_ENDPOINT_URL_S3: http://seaweedfs:8333\n      AWS_REGION: us-east-1\n"
        "      VAULT_ADDR: http://openbao:8200\n      BAO_ADDR: http://openbao:8200\n      S3_BUCKET: demo-chat-attachments\n"
    ),
}


def test_onprem_boto3_with_endpoint_url_is_not_an_aws_dependency(tmp_path):
    """Regression (ADR-0034): an on-prem app's boto3 + endpoint_url is an S3-compatible store
    (SeaweedFS), and its AWS_* variables are that store's settings, not IAM."""
    findings = scan_repo(_write(tmp_path, ONPREM_APP), "app", "onprem")
    s3 = [f for f in _by(findings, "sdk_call") if f.service == "s3"][0]
    assert s3.role == "object_store" and s3.cloud_dependency is False and s3.detail["compat"] == "s3-compatible"
    assert not [f for f in findings if f.role == "iam"]
    aws_vars = [f for f in findings if f.detail.get("name", "").startswith("AWS_")]
    assert aws_vars and all(f.role == "object_store" and not f.cloud_dependency for f in aws_vars)
    assert not any(f.cloud_dependency for f in findings)
    vault = [f for f in _by(findings, "sdk_call") if f.service == "hvac"][0]
    assert vault.role == "secrets" and vault.cloud_dependency is False
    env = {f.detail["name"]: f.role for f in _by(findings, "config_env")}
    assert env["VAULT_ADDR"] == env["BAO_ADDR"] == "secrets"


def test_same_code_under_aws_rules_is_an_aws_dependency(tmp_path):
    findings = scan_repo(_write(tmp_path, ONPREM_APP), "app", "aws")
    assert [f for f in findings if f.detail.get("name") == "AWS_ACCESS_KEY_ID" and f.role == "iam"]
    assert all(f.cloud_dependency for f in _by(findings, "sdk_call"))
    assert not [f for f in _by(findings, "sdk_call") if f.service == "hvac"]  # aws rules unchanged


def test_onprem_s3_endpoint_from_env_and_cloud_endpoint_literal(tmp_path):
    app = {
        "app/a.py": "import boto3\ns3 = boto3.client('s3')\n",
        ".env.example": "AWS_ENDPOINT_URL_S3=http://seaweedfs:8333\n",
    }
    s3 = [f for f in _by(scan_repo(_write(tmp_path / "env", app), "app", "onprem"), "sdk_call")][0]
    assert s3.cloud_dependency is False and s3.detail["endpoint_from"] == "env:AWS_ENDPOINT_URL_S3"
    # without any endpoint the client goes to AWS: still a cloud dependency on-prem
    bare = _write(tmp_path / "bare", {"app/a.py": app["app/a.py"]})
    assert _by(scan_repo(bare, "app", "onprem"), "sdk_call")[0].cloud_dependency is True
    literal = _write(tmp_path / "lit", {"app/a.py": "import boto3\ns3 = boto3.client('s3', endpoint_url='https://s3.ap-northeast-2.amazonaws.com')\n"})
    assert _by(scan_repo(literal, "app", "onprem"), "sdk_call")[0].cloud_dependency is True


def test_openbao_policy_text_and_json_forms():
    assert policy_paths('path "secret/data/a/*" {\n  capabilities = ["read", "list"]\n}\n') == {"secret/data/a/*": ["read", "list"]}
    assert policy_paths({"path": {"transit/encrypt/k": {"capabilities": ["update"]}}}) == {"transit/encrypt/k": ["update"]}


def test_onprem_file_adapter_follows_openbao_and_seaweedfs():
    adapter = deploy_adapter_for("onprem", FIXTURES / "exports" / "onprem-source")
    assert isinstance(adapter, OnpremFileAdapter)
    g = adapter.trace("chat")
    res = {(r.kind, r.name): r for r in g.resources}
    assert ("openbao_role", "kubernetes/chat") in res
    assert ("openbao_role", "kubernetes/other-app") not in res  # bound to another namespace
    secret = res[("openbao_secret", "secret/chat/*")]
    assert secret.role == "secrets" and secret.actions == ["read"] and "OpenBaoRole/kubernetes/chat → policy chat-read" in secret.via
    transit = res[("openbao_transit_key", "chat-attachments")]
    assert transit.role == "kms" and transit.actions == ["update"]
    assert not any("token/lookup-self" in r.name for r in g.resources)
    bucket = res[("seaweedfs_bucket", "demo-chat-attachments")]
    assert bucket.role == "object_store" and bucket.actions == ["List", "Read", "Write"]
    assert "SeaweedFSIdentity/client" in bucket.via and bucket.attributes["public"] == "False"
    assert bucket.attributes["referenced_by_env"] == "Deployment/chat:S3_BUCKET"
    # ESO's service account is used by no Deployment but bound by an OpenBao role
    assert ("openbao_secret", "secret/chat/db") in res and "ServiceAccount/external-secrets" in res[("openbao_secret", "secret/chat/db")].via
    assert any("missing-policy" in n for n in g.notes)
    assert any("bootstrap" in n and "연결 미확인" in n for n in g.notes)
    dump = json.dumps(g.as_dict())
    assert "fixture-not-a-real-value" not in dump and "fixture-client-access" not in dump
    assert g.source_kind == "onprem" and {"file:openbao.json", "file:s3.json"} <= set(g.api_calls)


def test_onprem_inventory_has_no_iam_item_and_marks_non_cloud_evidence(tmp_path, profiles):
    repo = _write(tmp_path / "repo", ONPREM_APP)
    with use_source_kind("onprem"):
        doc = build_inventory(
            app_id="demo-chat", repo=repo, app_path="app",
            source_env={"env_id": "onprem-a", "env_kind": "onprem", "namespace": "chat"},
            adapter=OnpremFileAdapter(FIXTURES / "exports" / "onprem-source"), profiles=profiles,
        )
    roles = {i["role"]: i for i in doc["items"]}
    assert doc["deploy"]["source_kind"] == "onprem"
    store = roles["object_store"]
    assert store["code_refs"] and all(r["source_kind"] == "onprem" and r["cloud_dependency"] is False for r in store["code_refs"])
    assert all(r["cloud_dependency"] is False for r in store["deploy_refs"])
    # no IAM item is derived from AWS_ACCESS_KEY_ID; the iam item (if any) comes from OpenBao / SeaweedFS identities only
    if "iam" in roles:
        assert not roles["iam"]["code_refs"]


def test_reconcile_marks_non_cloud_evidence_and_keeps_target_default_without_source_kind(tmp_path):
    findings = scan_repo(_write(tmp_path, ONPREM_APP), "app", "onprem")
    graph = OnpremFileAdapter(FIXTURES / "exports" / "onprem-source").trace("chat")
    store = [i for i in reconcile_module.reconcile(findings, graph, "onprem") if i.role == "object_store"][0]
    assert "퍼블릭 클라우드 의존이 아니다" in store.rationale
    assert all(r["source_kind"] == "onprem" for r in store.code_refs)


def test_reconcile_passes_source_kind_only_when_given_and_supported(tmp_path, monkeypatch):
    findings = scan_repo(_write(tmp_path, ONPREM_APP), "app", "onprem")
    seen: list = []

    def hub_grade(role, calls, env_names, env_kind, source_kind=None):
        seen.append(source_kind)
        return ("config_swap", "hub", False)

    monkeypatch.setattr(reconcile_module, "grade_role", hub_grade)
    monkeypatch.setattr(reconcile_module, "_GRADE_TAKES_SOURCE", True)
    reconcile_module.reconcile(findings, None, "aws", source_kind="onprem")
    assert seen and set(seen) == {"onprem"}
    seen.clear()
    reconcile_module.reconcile(findings, None, "aws")  # legacy: target default, re-graded by the caller
    assert set(seen) == {None}

    def legacy_grade(role, calls, env_names, env_kind):  # a catalog without the hub
        return ("config_swap", "legacy", False)

    monkeypatch.setattr(reconcile_module, "grade_role", legacy_grade)
    monkeypatch.setattr(reconcile_module, "_GRADE_TAKES_SOURCE", False)
    assert reconcile_module.reconcile(findings, None, "aws", source_kind="onprem")


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_trace_selects_the_kinds_adapter(tmp_path, capsys):
    out = tmp_path / "graph.json"
    assert main(["trace", "--source-kind", "gcp", "--namespace", "chat", "--exports", str(FIXTURES / "exports" / "gcp"), "--output", str(out)]) == 0
    graph = json.loads(out.read_text(encoding="utf-8"))
    assert graph["source_kind"] == "gcp" and any(r["kind"] == "gcs_bucket" for r in graph["resources"])


def test_cli_scan_source_kind_selects_rules_and_reports_unsupported(tmp_path, capsys, profiles):
    repo = _write(tmp_path / "repo", ONPREM_APP)
    base = ["scan", "--app-id", "demo-chat", "--repo", str(repo), "--app-path", "app", "--source-env", "onprem-a", "--namespace", "chat",
            "--exports", str(FIXTURES / "exports" / "onprem-source")]
    for p in profiles:
        base += ["--profile", str(p)]
    out = tmp_path / "inv.json"
    assert main([*base, "--source-kind", "onprem", "--target-kind", "onprem", "--output", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    refs = [r for i in doc["items"] for r in i["code_refs"]]
    assert refs and {r["source_kind"] for r in refs} == {"onprem"}
    assert not [i for i in doc["items"] if i["role"] == "iam" and i["code_refs"]]
    # live tracing is not registered for onprem: UNSUPPORTED, nothing written
    assert main([*base, "--source-kind", "onprem", "--live", "--output", str(tmp_path / "x.json")]) == 3
    assert UNSUPPORTED in capsys.readouterr().err and not (tmp_path / "x.json").exists()
    assert main([*base, "--source-kind", "gcp", "--aws-live", "--output", str(tmp_path / "y.json")]) == 2
