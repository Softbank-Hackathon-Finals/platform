"""infra/envs/target-backup: SeaweedFS registration, CNPG manifests and the verification verdict."""

import importlib.util
import io
import json
import sys
from pathlib import Path
from string import Template
from unittest.mock import patch

from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parents[3] / "infra/envs/target-backup"


def load(name):
    spec = importlib.util.spec_from_file_location(f"target_backup_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(HERE), *sys.path]):
        spec.loader.exec_module(module)
    return module


render, verify = load("render"), load("verify")
WRITER = "arn:aws:iam::000000000000:user/t35-backup-writer"
READER = "arn:aws:iam::000000000000:user/t35-backup-reader"


def test_identities_are_bucket_scoped_and_the_reader_cannot_write():
    writer, reader = render.identities()
    assert writer == {"name": "t35-backup-writer", "actions": ["Read:bravo-backup", "Write:bravo-backup", "List:bravo-backup"]}
    assert reader == {"name": "t35-backup-reader", "actions": ["Read:bravo-backup", "List:bravo-backup"]}


def test_policy_denies_deletion_to_everyone():
    deletion, protect = render.bucket_policy()["Statement"]
    for statement in (deletion, protect):
        assert statement["Effect"] == "Deny" and statement["Principal"] == "*" and "Condition" not in statement
    assert set(deletion["Action"]) == {"s3:DeleteObject", "s3:DeleteObjectVersion"}
    assert deletion["Resource"] == [Template("arn:aws:s3:::${b}/*").substitute(b="bravo-backup")]
    assert set(protect["Action"]) == {"s3:PutBucketVersioning", "s3:DeleteBucket"}


def test_stores_use_tls_and_separate_identities():
    writer, namespace, reader = render.stores()
    for store, secret, ns in ((writer, "t35-backup-writer", "target-dev"), (reader, "t35-backup-reader", "t35-restore-check")):
        config = store["spec"]["configuration"]
        assert store["metadata"]["namespace"] == ns
        assert config["endpointURL"] == "https://seaweedfs.target-dev.svc:8333"
        assert config["endpointCA"] == {"name": "t35-backup-ca", "key": "ca.crt"}
        assert config["s3Credentials"]["accessKeyId"]["name"] == secret
        assert config["destinationPath"] == "s3://bravo-backup/t35/target-db/"
    assert namespace["metadata"]["name"] == "t35-restore-check"


def test_cluster_pods_carry_the_store_client_label():
    patch_spec = render.archive()["spec"]
    assert patch_spec["inheritedMetadata"] == {"labels": {"bravo-object-client": "true"}}
    assert patch_spec["plugins"][0]["parameters"] == {"barmanObjectName": "target-db-backup"}
    (restored,) = render.restore("20261105T010000", "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:" + "a" * 64,
                                 "local-path", 1)
    assert restored["metadata"]["namespace"] == "t35-restore-check"
    assert restored["spec"]["inheritedMetadata"] == {"labels": {"bravo-object-client": "true"}}
    assert "plugins" not in restored["spec"]  # never archives into the source prefix
    assert render.backup("r1")[0]["metadata"]["name"] == "target-db-t35-r1"


def test_verify_compares_policy_regardless_of_list_or_string():
    policy = render.bucket_policy()
    returned = {"Version": "2012-10-17", "Statement": [dict(s, Resource=s["Resource"][0]) for s in policy["Statement"]]}
    assert verify.normalized(returned) == verify.normalized(policy)
    weakened = {"Version": "2012-10-17", "Statement": policy["Statement"][1:]}
    assert verify.normalized(weakened) != verify.normalized(policy)


class FakeS3:
    """Records every call; policy and versioning behave like a bucket that enforces the rendered policy."""

    def __init__(self, calls, policy=True, policy_error=None, deny_delete=True):
        self.calls, self.policy, self.policy_error, self.deny_delete = calls, policy, policy_error, deny_delete

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append(name)
            if name == "get_bucket_versioning":
                return {"Status": "Enabled"}
            if name == "get_bucket_policy":
                if self.policy_error:
                    raise ClientError({"Error": {"Code": self.policy_error}}, name)
                return {"Policy": json.dumps(render.bucket_policy())}
            if name.startswith("delete") or name in ("put_bucket_versioning", "put_bucket_policy"):
                if self.deny_delete:
                    raise ClientError({"Error": {"Code": "AccessDenied"}}, name)
                return {}
            if name == "put_object":
                if kw["Key"].endswith(".reader"):
                    raise ClientError({"Error": {"Code": "AccessDenied"}}, name)
                return {"VersionId": "v1"}
            if name == "get_object":
                return {"Body": io.BytesIO(b"t35")}
            if name == "list_object_versions":
                return {"Versions": [{"Key": kw["Prefix"], "VersionId": "v1"}]}
            return {}
        return call


def run_check(labels, disposable=False, **fake):
    calls = []
    with patch.object(verify, "client", lambda endpoint, label, ca: FakeS3(calls, **fake)):
        out = verify.check("https://x", labels, disposable)
    return out, calls


def test_registered_bucket_is_never_asked_to_change_protection():
    # Review reproduction: protection-changing calls used to run on the registered bucket
    out, calls = run_check(["app", "executor"])
    assert not {"put_bucket_versioning", "put_bucket_policy", "delete_bucket_policy"} & set(calls)
    assert verify.status(out, ["app", "executor"], False) == "PASS"
    out, calls = run_check(["app", "executor"], disposable=True)
    assert "delete_bucket_policy" in calls
    assert verify.status(out, ["app", "executor"], True) == "PASS"


def test_missing_or_unreadable_policy_stops_before_any_probe():
    out, calls = run_check(["app", "executor"], policy_error="NoSuchBucketPolicy")
    assert out["policy_matches"] is False and "put_object" not in calls
    assert verify.status(out, ["app", "executor"], False) == "FAIL"
    out, calls = run_check(["app", "executor"], policy_error="AccessDenied")
    assert out["policy_matches"] is None and "put_object" not in calls
    assert verify.status(out, ["app", "executor"], False) == "UNKNOWN"


def test_app_and_executor_must_be_shown_refused():
    out, _ = run_check(["app", "executor"], deny_delete=False)
    assert out["executor.delete_version"] == "ok"
    assert verify.status(out, ["app", "executor"], False) == "FAIL"
    out, _ = run_check([])
    assert verify.status(out, [], False) == "UNKNOWN"


def test_verify_status():
    labels = ["app", "executor"]
    good = dict(verify.EXPECTED, versioning_enabled=True, policy_matches=True, probe_intact=True,
                **{f"{label}.{call}": "denied" for label in labels for call in ("delete", "delete_version")})
    assert verify.status(good, labels, False) == "PASS"
    assert verify.status(dict(good, **{"writer.delete": "ok"}), labels, False) == "FAIL"
    assert verify.status(dict(good, policy_matches=False), labels, False) == "FAIL"
    assert verify.status(dict(good, **{"reader.get": "error:EndpointConnectionError"}), labels, False) == "UNKNOWN"
    assert verify.status(dict(good, **{"executor.delete": "error:Timeout"}), labels, False) == "UNKNOWN"
    assert verify.status(good, ["app"], False) == "UNKNOWN"  # the Executor was not checked
    changed = dict(good, protection_after={"versioning_enabled": False, "policy_matches": True},
                   **{f"{w}.{c}": "denied" for w in ("writer", *labels) for c in verify.PROTECTION_CHANGES})
    assert verify.status(changed, labels, True) == "FAIL"
