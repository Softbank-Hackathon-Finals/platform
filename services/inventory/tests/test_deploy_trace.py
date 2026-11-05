import json

import pytest

from bravo_inventory.deploy_trace import READ_ONLY_API, Boto3Adapter, FileAdapter


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
    # readback keys are allowlisted; referenced_by_env is the adapter's own (env *name* only)
    assert set(bucket.attributes) == {"arn", "public", "region", "referenced_by_env"}


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

    def get_bucket_location(self, Bucket):
        self.log.append("get_bucket_location:" + Bucket)
        return {"LocationConstraint": "ap-northeast-2"}

    def describe_secret(self, SecretId):
        self.log.append("describe_secret:" + SecretId)
        return {"Name": "demo-chat/db", "RotationEnabled": True, "Tags": [{"Key": "owner", "Value": "x@example.invalid"}],
                "KmsKeyId": "arn:aws:kms:ap-northeast-2:000000000000:key/00000000-0000-0000-0000-00000000abcd"}

    def describe_key(self, KeyId):
        self.log.append("describe_key:" + KeyId)
        return {"KeyMetadata": {"KeyManager": "CUSTOMER", "KeyState": "Enabled", "KeySpec": "SYMMETRIC_DEFAULT", "Description": "not copied"}}

    def delete_bucket(self, Bucket):  # must never be reachable
        raise AssertionError("write API called")


class _FakeSession:
    def __init__(self):
        self.log = []
        self.regions = []

    def client(self, name, region_name=None):
        self.regions.append((name, region_name))
        return _FakeClient(self.log)


def test_boto3_adapter_only_uses_read_only_allowlist(exports):
    session = _FakeSession()
    g = Boto3Adapter(exports, session=session).trace("chat")
    assert "iam:GetRolePolicy" in g.api_calls and "s3:GetBucketPolicyStatus" in g.api_calls
    assert all(c in READ_ONLY_API or c.startswith("file:") for c in g.api_calls)
    live = [r for r in g.resources if r.name == "live-bucket.example.invalid"][0]
    assert live.attributes["public"] == "True"


def test_boto3_adapter_describes_secrets_keys_and_bucket_location(exports):
    # Real shakedown 2026-11-06: DescribeSecret · DescribeKey · GetBucketLocation were allowlisted
    # but never called, so live secret/KMS attributes (rotation, encrypting key) stayed empty.
    session = _FakeSession()
    g = Boto3Adapter(exports, session=session).trace("chat")
    assert {"secretsmanager:DescribeSecret", "kms:DescribeKey", "s3:GetBucketLocation"} <= set(g.api_calls)
    secret = [r for r in g.resources if r.kind == "secret"][0]
    assert secret.attributes["rotation_enabled"] == "True" and secret.attributes["kms_key_id"].endswith("key/00000000-0000-0000-0000-00000000abcd")
    key = [r for r in g.resources if r.kind == "kms_key"][0]
    assert {k: key.attributes[k] for k in ("key_manager", "key_state", "key_spec")} == {"key_manager": "CUSTOMER", "key_state": "Enabled", "key_spec": "SYMMETRIC_DEFAULT"}
    assert "not copied" not in json.dumps(g.as_dict()) and "x@example.invalid" not in json.dumps(g.as_dict())
    bucket = [r for r in g.resources if r.name == "demo-chat-attachments.example.invalid"][0]
    assert bucket.attributes["region"] == "ap-northeast-2"
    assert ("secretsmanager", "ap-northeast-2") in session.regions and ("kms", "ap-northeast-2") in session.regions
    # one observation per resource even when two policy ARNs (bucket, bucket/*) name the same bucket
    assert session.log.count("get_bucket_policy_status:demo-chat-attachments.example.invalid") == 1


def test_same_resource_named_by_two_arns_is_one_deploy_ref(exports):
    # Real shakedown: the bucket ARN (ListBucket) and the bucket/* object ARN (Get/PutObject) showed up
    # as two s3_bucket refs for one bucket.
    g = FileAdapter(exports).trace("chat")
    buckets = [r for r in g.resources if r.kind == "s3_bucket" and r.name == "demo-chat-attachments.example.invalid"]
    assert len(buckets) == 1 and buckets[0].actions == ["s3:GetObject", "s3:PutObject"]


def test_irsa_service_account_without_a_deployment_is_traced(exports, tmp_path):
    # Real shakedown: the ESO SecretStore's IRSA service account is used by no Deployment, so the
    # Secrets Manager entry it syncs into the app (ADMIN_TOKEN) was missing from the inventory.
    d = _exports_with(exports, tmp_path)
    sa = json.loads((d / "serviceaccounts.json").read_text(encoding="utf-8"))
    sa["items"].append({"metadata": {"name": "secret-reader", "namespace": "chat", "annotations": {"eks.amazonaws.com/role-arn": "arn:aws:iam::000000000000:role/chat-eso"}}})
    (d / "serviceaccounts.json").write_text(json.dumps(sa), encoding="utf-8")
    aws = json.loads((d / "aws.json").read_text(encoding="utf-8"))
    aws["roles"]["arn:aws:iam::000000000000:role/chat-eso"] = {"policies": [{"name": "control", "statements": [
        {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": "arn:aws:secretsmanager:ap-northeast-2:000000000000:secret:demo-chat/control-XyZ123"}]}]}
    (d / "aws.json").write_text(json.dumps(aws), encoding="utf-8")
    g = FileAdapter(d).trace("chat")
    control = [r for r in g.resources if r.kind == "secret" and r.name == "demo-chat/control-XyZ123"]
    assert len(control) == 1 and control[0].via.startswith("ServiceAccount/secret-reader")
    assert ("iam_role", "arn:aws:iam::000000000000:role/chat-eso") in {(r.kind, r.name) for r in g.resources}


def test_env_reference_to_a_resource_is_recorded_by_name_only(exports):
    # The literal env value is compared with the resource name inside the adapter; only the env
    # *name* is written (values are never copied). Secrets Manager's 6-char ARN suffix is ignored.
    g = FileAdapter(exports).trace("chat")
    by = {(r.kind, r.name): r for r in g.resources}
    assert by[("s3_bucket", "demo-chat-attachments.example.invalid")].attributes["referenced_by_env"] == "Deployment/chat:S3_BUCKET"
    assert by[("secret", "demo-chat/db-AbCdEf")].attributes["referenced_by_env"] == "Deployment/chat:DB_SECRET_ID"
    assert "referenced_by_env" not in by[("s3_bucket", "chat-audit-archive.example.invalid")].attributes
    assert "chat-pooler-rw" not in json.dumps(g.as_dict())


def test_file_api_calls_list_only_files_that_were_read(exports, tmp_path):
    # Real shakedown: without aws.json the output still claimed `file:aws.json` was read.
    (tmp_path / "deployments.json").write_bytes((exports / "deployments.json").read_bytes())
    assert FileAdapter(tmp_path).trace("chat").api_calls == ["file:deployments.json"]


def test_boto3_adapter_refuses_unlisted_api(exports):
    adapter = Boto3Adapter(exports, session=_FakeSession())
    with pytest.raises(PermissionError):
        adapter._call(_FakeClient([]), "s3:DeleteBucket", Bucket="x")
