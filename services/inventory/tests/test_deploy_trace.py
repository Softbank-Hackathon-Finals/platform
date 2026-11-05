import json

import pytest

from bravo_inventory.deploy_trace import Boto3Adapter, FileAdapter


def test_file_adapter_follows_sa_to_role_to_resources(exports):
    g = FileAdapter(exports).trace("chat")
    kinds = {(r.kind, r.name) for r in g.resources}
    assert ("iam_role", "arn:aws:iam::000000000000:role/chat-irsa") in kinds
    assert ("s3_bucket", "demo-chat-attachments.example.invalid") in kinds
    assert ("secret", "demo-chat/db-AbCdEf") in kinds
    assert ("kms_key", "00000000-0000-0000-0000-00000000abcd") in kinds
    assert ("cnpg_cluster", "chat-db") in kinds
    bucket = [r for r in g.resources if r.kind == "s3_bucket" and r.name == "demo-chat-attachments.example.invalid"][0]
    assert bucket.attributes["public"] == "False" and "ServiceAccount/chat" in bucket.via


def test_env_values_are_never_copied_only_names_and_sources(exports):
    g = FileAdapter(exports).trace("chat")
    w = g.workloads[0]
    assert "ADMIN_TOKEN" in w["env"] and w["env_sources"]["ADMIN_TOKEN"] == "secretKeyRef"
    assert w["env_sources"]["DB_HOST"] == "literal"
    assert "env_values" not in w
    assert "chat-pooler-rw" not in json.dumps(w)  # literal values are not echoed, even harmless ones


def _exports_with(exports, tmp_path, env=(), bucket_extra=None):
    for name in ("deployments.json", "serviceaccounts.json", "clusters.json", "aws.json"):
        (tmp_path / name).write_bytes((exports / name).read_bytes())
    dep = json.loads((tmp_path / "deployments.json").read_text(encoding="utf-8"))
    dep["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"].extend(env)
    (tmp_path / "deployments.json").write_text(json.dumps(dep), encoding="utf-8")
    if bucket_extra:
        aws = json.loads((tmp_path / "aws.json").read_text(encoding="utf-8"))
        aws["buckets"]["demo-chat-attachments.example.invalid"].update(bucket_extra)
        (tmp_path / "aws.json").write_text(json.dumps(aws), encoding="utf-8")
    return tmp_path


def test_dsn_in_deployment_env_is_reduced_to_host(exports, tmp_path):
    # Regression for review P1: a Deployment carrying a connection string must not leak the
    # password into deploy.workloads, and the endpoint must survive as scheme://host[:port].
    pw = "FIXTURE-PW-9f3a"
    d = _exports_with(exports, tmp_path, env=[
        {"name": "DATABASE_URL", "value": f"postgresql://chat:{pw}@chat-pooler-rw:5432/chat"},
        {"name": "S3_ENDPOINT_URL", "value": f"http://AKIAFIXTURE0000000:{pw}@minio.local:9000/"},
        {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
    ])
    g = FileAdapter(d).trace("chat")
    dump = json.dumps(g.as_dict())
    assert pw not in dump and "AKIAFIXTURE" not in dump
    w = g.workloads[0]
    assert w["endpoints"] == {"DATABASE_URL": "postgresql://chat-pooler-rw:5432", "S3_ENDPOINT_URL": "http://minio.local:9000"}
    assert w["env_sources"]["DATABASE_URL"] == "literal" and w["env_sources"]["POD_IP"] == "fieldRef"


def test_resource_attributes_are_allowlisted(exports, tmp_path):
    d = _exports_with(exports, tmp_path, bucket_extra={"policy_document": "{\"Statement\": []}", "owner_email": "x@example.invalid"})
    g = FileAdapter(d).trace("chat")
    bucket = [r for r in g.resources if r.kind == "s3_bucket" and r.name == "demo-chat-attachments.example.invalid"][0]
    assert set(bucket.attributes) == {"arn", "public", "region"}


class _FakeClient:
    def __init__(self, log):
        self.log = log

    def list_attached_role_policies(self, RoleName):
        self.log.append("list_attached_role_policies")
        return {"AttachedPolicies": []}

    def list_role_policies(self, RoleName):
        self.log.append("list_role_policies")
        return {"PolicyNames": ["inline"]}

    def get_role_policy(self, RoleName, PolicyName):
        self.log.append("get_role_policy")
        return {"PolicyDocument": {"Statement": {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::live-bucket.example.invalid/*"}}}

    def get_bucket_policy_status(self, Bucket):
        self.log.append("get_bucket_policy_status:" + Bucket)
        return {"PolicyStatus": {"IsPublic": True}}

    def delete_bucket(self, Bucket):  # must never be reachable
        raise AssertionError("write API called")


class _FakeSession:
    def __init__(self):
        self.log = []

    def client(self, name):
        return _FakeClient(self.log)


def test_boto3_adapter_only_uses_read_only_allowlist(exports):
    session = _FakeSession()
    g = Boto3Adapter(exports, session=session).trace("chat")
    assert "iam:GetRolePolicy" in g.api_calls and "s3:GetBucketPolicyStatus" in g.api_calls
    assert all(c in {"iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies", "s3:GetBucketPolicyStatus"} or c.startswith("file:") for c in g.api_calls)
    live = [r for r in g.resources if r.name == "live-bucket.example.invalid"][0]
    assert live.attributes["public"] == "True"


def test_boto3_adapter_refuses_unlisted_api(exports):
    adapter = Boto3Adapter(exports, session=_FakeSession())
    with pytest.raises(PermissionError):
        adapter._call(_FakeClient([]), "s3:DeleteBucket", Bucket="x")
