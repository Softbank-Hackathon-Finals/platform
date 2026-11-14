"""`source.neutral_export`: each source kind → neutral model."""

import json

import pytest
from neutral_fixtures import APP, BUCKET, SOURCE_PRINCIPALS, SOURCES

from rehearsal_runner.neutral import (
    Anonymous,
    Grant,
    NeutralError,
    Unresolved,
    Workload,
    export_aws_secret,
    export_gcp_secret,
    export_gcs_iam_policy,
    export_object_policy,
    export_openbao_secret,
    export_s3_bucket_policy,
    export_seaweedfs_identities,
)
from rehearsal_runner.neutral.export import S3_ARN

ALL = ("read", "write", "list", "delete")


@pytest.mark.parametrize("kind", ["aws", "gcp", "onprem"])
def test_each_source_kind_exports_the_app_the_public_and_an_unresolved_principal(kind):
    policy = export_object_policy(kind, SOURCES[kind], bucket=BUCKET, principals=SOURCE_PRINCIPALS[kind])
    by_principal = {type(g.principal): g for g in policy.grants}
    assert by_principal[Workload] == Grant(APP, ALL)
    assert by_principal[Anonymous] == Grant(Anonymous(), ("read",))
    assert by_principal[Unresolved].actions == ("read",)
    assert policy.public_read and not policy.public_list
    assert len(policy.unresolved) == 1
    # account / project identifiers are masked in unresolved references
    assert "111122223333" not in policy.unresolved[0] and "example-project" not in policy.unresolved[0]


def test_bucket_is_inferred_when_the_policy_names_exactly_one():
    assert export_s3_bucket_policy(SOURCES["aws"]).bucket == BUCKET
    assert export_gcs_iam_policy(SOURCES["gcp"]).bucket == BUCKET
    assert export_seaweedfs_identities(SOURCES["onprem"]).bucket == BUCKET


def test_s3_tls_deny_and_anonymous_deny_are_understood():
    text = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": f"{S3_ARN}{BUCKET}/*"},
        {"Effect": "Deny", "Principal": "*", "Action": "s3:GetObject", "Resource": f"{S3_ARN}{BUCKET}/*",
         "Condition": {"StringEquals": {"aws:PrincipalType": "Anonymous"}}},
        {"Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [f"{S3_ARN}{BUCKET}", f"{S3_ARN}{BUCKET}/*"],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]})
    policy = export_s3_bucket_policy(text)
    assert policy.grants == () and not policy.public_read and policy.tls_required


@pytest.mark.parametrize("statement", [
    # a Deny to everyone also blocks identities outside this policy: not expressible additively
    {"Effect": "Deny", "Principal": "*", "Action": "s3:DeleteObject", "Resource": f"{S3_ARN}{BUCKET}/*"},
    {"Effect": "Deny", "Principal": {"AWS": "arn:aws:iam::111122223333:role/unknown"}, "Action": "s3:GetObject",
     "Resource": f"{S3_ARN}{BUCKET}/*"},
    {"Effect": "Allow", "NotPrincipal": "*", "Action": "s3:GetObject", "Resource": f"{S3_ARN}{BUCKET}/*"},
    {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": f"{S3_ARN}{BUCKET}/a?c*"},
])
def test_s3_statements_that_would_change_meaning_are_refused(statement):
    with pytest.raises(NeutralError):
        export_s3_bucket_policy(json.dumps({"Version": "2012-10-17", "Statement": [statement]}), bucket=BUCKET)


def test_conditional_or_partly_understood_grants_become_unresolved_and_prefixes_are_kept():
    text = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "Cond", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::111122223333:role/demo-chat"},
         "Action": "s3:GetObject", "Resource": f"{S3_ARN}{BUCKET}/*",
         "Condition": {"IpAddress": {"aws:SourceIp": "192.0.2.0/24"}}},
        {"Sid": "Acl", "Effect": "Allow", "Principal": "*", "Action": ["s3:GetObject", "s3:PutObjectAcl"],
         "Resource": f"{S3_ARN}{BUCKET}/*"},
        {"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::111122223333:role/demo-chat"},
         "Action": "s3:PutObject", "Resource": f"{S3_ARN}{BUCKET}/uploads/*"}]})
    policy = export_s3_bucket_policy(text, principals={"arn:aws:iam::111122223333:role/demo-chat": APP})
    assert not policy.public_read  # the anonymous grant was only partly understood: not granted as public
    assert Grant(APP, ("write",), "uploads/") in policy.grants
    assert sorted(policy.unresolved) == ["anonymous#action:Acl",
                                         "system:serviceaccount:demo-chat:demo-chat#condition:Cond"]


def test_kubernetes_subjects_and_gke_members_map_without_a_trace():
    text = json.dumps({"bindings": [
        {"role": "roles/storage.objectViewer", "members": ["serviceAccount:example-project.svc.id.goog[demo-chat/demo-chat]"]},
        {"role": "roles/storage.objectCreator", "members": ["allUsers"], "condition": {"title": "t", "expression": "true"}}]})
    policy = export_gcs_iam_policy(text, bucket=BUCKET)
    assert Grant(APP, ("read", "list")) in policy.grants
    assert policy.unresolved == ("anonymous#condition",) and not policy.public_read


def test_seaweedfs_credentials_never_enter_the_model_and_other_buckets_are_ignored():
    text = json.dumps({"identities": [
        {"name": "demo-chat", "credentials": [{"accessKey": "FAKECLIENT00000001", "secretKey": "fake-" + "1" * 30}],
         "actions": [f"Read:{BUCKET}", "Admin:other-bucket"]}]})
    policy = export_seaweedfs_identities(text, bucket=BUCKET, principals={"demo-chat": APP})
    assert policy.grants == (Grant(APP, ("read",)),)
    assert "FAKECLIENT" not in json.dumps(policy.to_dict())


def test_unknown_gcs_role_is_refused():
    with pytest.raises(NeutralError, match="ROLE_UNTRANSLATABLE"):
        export_gcs_iam_policy(json.dumps({"bindings": [{"role": "roles/owner", "members": ["allUsers"]}]}), bucket=BUCKET)


# --- secrets ------------------------------------------------------------------------------------

CONSUMERS = ["system:serviceaccount:demo-chat:demo-chat", "arn:aws:iam::111122223333:role/ops"]


@pytest.mark.parametrize("kind, export, metadata, ref, rotation", [
    ("aws", export_aws_secret, {"Name": "prod/demo-chat/db", "ARN": "arn:aws:secretsmanager:us-east-1:111122223333:secret:prod/demo-chat/db-AbCdEf",
                                "RotationEnabled": True}, "prod/demo-chat/db", True),
    ("gcp", export_gcp_secret, {"name": "projects/example-project/secrets/demo-chat-db"}, "demo-chat-db", False),
    ("onprem", export_openbao_secret, {"mount": "secret", "path": "apps/demo-chat/db",
                                       "custom_metadata": {"rotation_required": "true"}}, "apps/demo-chat/db", True),
])
def test_secret_metadata_exports_without_values(kind, export, metadata, ref, rotation):
    binding = export(metadata, keys=["username", "password"], consumers=CONSUMERS)
    assert binding.source.kind == kind and binding.source.ref == ref
    assert binding.consumers == (APP,) and binding.rotation_required is rotation
    assert binding.unresolved == ("arn:aws:iam::***:role/ops",)
    assert "111122223333" not in json.dumps(binding.to_dict())


@pytest.mark.parametrize("export, metadata", [
    (export_aws_secret, {"Name": "db", "SecretString": "x"}),
    (export_gcp_secret, {"name": "projects/p1234/secrets/db", "payload": {"data": "eA=="}}),
    (export_openbao_secret, {"path": "apps/db", "data": {"password": "x"}}),
])
def test_secret_values_are_refused_not_stripped(export, metadata):
    with pytest.raises(NeutralError, match="SECRET_VALUE_PRESENT"):
        export(metadata, keys=["password"], consumers=CONSUMERS)


def test_secret_without_any_mapped_consumer_is_refused():
    with pytest.raises(NeutralError, match="CONSUMER_UNRESOLVED"):
        export_aws_secret({"Name": "db"}, keys=["password"], consumers=["arn:aws:iam::111122223333:role/ops"])
