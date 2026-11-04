"""T35 source backup verification: a policy that does not block deletion and a probe that did not run are never a pass."""
import argparse
import importlib.util
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("source_backup_verify", ROOT / "infra/envs/source-backup/verify.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

BUCKET = "bravo-source-backup-111122223333"
ARN = f"arn:aws:s3:::{BUCKET}"
CUSTODIANS = ["arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_Admin_0123456789abcdef"]
WRITER = "arn:aws:iam::111122223333:role/bravo-source-backup-writer"
READER = "arn:aws:iam::111122223333:role/bravo-source-backup-reader"
APP = "arn:aws:iam::111122223333:role/bravo-source-workloads-app"
DENIED = "aws: [ERROR]: An error occurred (AccessDenied) when calling the DeleteObject operation: Access Denied"


def stack_policy():
    """The policy main.tf writes."""
    nc = {"ArnNotEquals": {"aws:PrincipalArn": CUSTODIANS}}
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": [ARN, ARN + "/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyBackupDeletionExceptCustodians", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"], "Resource": ARN + "/*", "Condition": nc},
        {"Sid": "DenyProtectionChangesExceptCustodians", "Effect": "Deny", "Principal": "*",
         "Action": sorted(verify.PROTECT_ACTIONS), "Resource": ARN, "Condition": nc}]}


def log(principal, version_exit=254, object_exit=254, lines=(DENIED, DENIED)):
    return f"principal={principal}\n{lines[0]}\nversion_exit={version_exit}\n{lines[1]}\nobject_exit={object_exit}\n"


def session(role_name):
    return f"arn:aws:sts::111122223333:assumed-role/{role_name}/botocore-session-1793780000"


class Policy(unittest.TestCase):
    def test_stack_policy_and_normalized_forms_pass(self):
        self.assertTrue(verify.guarded_policy(stack_policy(), BUCKET, CUSTODIANS))
        single = stack_policy()
        for s in single["Statement"][1:]:
            s["Condition"]["ArnNotEquals"]["aws:PrincipalArn"] = CUSTODIANS[0]  # one value may come back as a string
        single["Statement"][0]["Principal"] = {"AWS": "*"}
        self.assertTrue(verify.guarded_policy(single, BUCKET, CUSTODIANS))

    def test_policies_that_do_not_block_deletion_fail(self):
        # Review reproduction: same Sids, Deny and exception, but GetObject on another prefix
        changed = stack_policy()
        for s in changed["Statement"][1:]:
            s["Action"], s["Resource"] = "s3:GetObject", ARN + "/other/*"
        mutations = [changed]
        for change in (
            lambda p: p["Statement"][1].update(Action=["s3:DeleteObject"]),
            lambda p: p["Statement"][2]["Action"].remove("s3:PutBucketPolicy"),
            lambda p: p["Statement"][1].update(Resource="arn:aws:s3:::other-bucket/*"),
            lambda p: p["Statement"][1].update(Principal={"AWS": APP}),
            lambda p: p["Statement"][1]["Condition"]["ArnNotEquals"].update({"aws:PrincipalArn": CUSTODIANS + [APP]}),
            lambda p: p["Statement"][1].update(Condition={"ArnNotLike": {"aws:PrincipalArn": "*"}}),
            lambda p: p["Statement"][1].update(NotAction=["s3:GetObject"]),
            lambda p: p["Statement"].append({"Sid": "AllowApp", "Effect": "Allow", "Principal": {"AWS": APP},
                                             "Action": "s3:*", "Resource": ARN + "/*"}),
            lambda p: p["Statement"].pop(0),
            lambda p: p["Statement"][1].update(Sid="DenySomethingElse"),
            lambda p: p["Statement"].append(dict(p["Statement"][1])),
            lambda p: p["Statement"][0].update(Condition={"Bool": {"aws:SecureTransport": "true"}}),
        ):
            p = stack_policy()
            change(p)
            mutations.append(p)
        for p in mutations:
            self.assertFalse(verify.guarded_policy(p, BUCKET, CUSTODIANS), json.dumps(p)[:200])


class Verdict(unittest.TestCase):
    def test_only_the_registered_role_explicitly_denied_twice_counts(self):
        self.assertEqual(verify.verdict(log(session("bravo-source-backup-writer")), WRITER), "DENIED")
        self.assertEqual(verify.verdict(log(session("bravo-source-workloads-app")), WRITER), "UNKNOWN_ROLE_MISMATCH")
        self.assertEqual(verify.verdict(log(""), WRITER), "UNKNOWN_NO_CREDENTIALS")
        self.assertEqual(verify.verdict(log("None"), WRITER), "UNKNOWN_NO_CREDENTIALS")
        other = "aws: [ERROR]: An error occurred (NoSuchKey) when calling the DeleteObject operation"
        self.assertEqual(verify.verdict(log(session("bravo-source-backup-writer"), lines=(DENIED, other)), WRITER),
                         "UNKNOWN_UNCLEAR")
        self.assertEqual(verify.verdict(log(session("bravo-source-backup-writer"), object_exit=0), WRITER), "DELETED")
        self.assertEqual(verify.verdict("principal=x\n", WRITER), "UNKNOWN_UNCLEAR")
        # Another account's role with the same name is not the registered role
        self.assertFalse(verify.same_role(session("bravo-source-backup-writer").replace("111122223333", "123456789012"), WRITER))
        # A role with a path (IAM Identity Center) matches by account and name
        self.assertTrue(verify.same_role(session("AWSReservedSSO_Admin_0123456789abcdef"), CUSTODIANS[0]))


def fake(sa_stderr=None, logs=None, deleted=False):
    """Fake aws/kubectl for probe(): ServiceAccount lookup result and per-namespace Pod logs."""
    created = {}

    def res(out="", code=0, err=""):
        return types.SimpleNamespace(returncode=code, stdout=out if isinstance(out, str) else json.dumps(out), stderr=err)

    def run(cmd, check=True):
        c = " ".join(cmd)
        if "put-object" in c:
            return res({"VersionId": "v1"})
        if "get serviceaccount" in c:
            return res("", 1, sa_stderr) if sa_stderr else res("serviceaccount found")
        if "jsonpath={.status.phase}" in c:
            return res("Succeeded")
        if " logs " in f" {c} ":
            return res(logs[cmd[cmd.index("--namespace") + 1]])
        if "head-object" in c:
            return res("{}", 254 if deleted else 0)
        if "list-object-versions" in c:
            return res({"Versions": [{"Key": cmd[cmd.index("--prefix") + 1], "VersionId": "v1"}]})
        if "delete-object" in c or ("delete" in cmd and "pod" in cmd):
            return res("")
        raise AssertionError(c)

    def create(cmd, input=None, **kw):
        created[json.loads(input)["metadata"]["namespace"]] = json.loads(input)
        return res("")
    return run, create


REG = {"bucket": BUCKET, "region": "ap-northeast-2", "destination": f"s3://{BUCKET}/t35/source-db/",
       "writer": {"namespace": "source-dev", "name": "source-db"}, "reader": {"namespace": "t35-restore-check", "name": "source-db-restore"},
       "writer_role_arn": WRITER, "reader_role_arn": READER}
SUBJECTS = {"writer": ("source-dev", "source-db", WRITER), "reader": ("t35-restore-check", "source-db-restore", READER),
            "app": ("app-ns", "demo-chat", APP)}
READBACK = {"versioning_enabled": True, "policy_matches_stack": True}


class Probe(unittest.TestCase):
    def probe(self, **kw):
        run, create = fake(**kw)
        with patch.object(verify, "run", run), patch.object(verify.subprocess, "run", create), patch.object(verify.time, "sleep"):
            return verify.probe(["aws"], ["kubectl"], REG, SUBJECTS)

    def test_forbidden_lookups_are_unknown_not_pass(self):
        # Review reproduction: every ServiceAccount lookup Forbidden used to pass with exit 0
        result = self.probe(sa_stderr='Error from server (Forbidden): serviceaccounts "x" is forbidden')
        self.assertEqual({result[k] for k in SUBJECTS}, {"UNKNOWN_SA_LOOKUP_FAILED"})
        self.assertEqual(verify.status(READBACK, result, "final"), "UNKNOWN")
        self.assertEqual(verify.status(READBACK, result, "before-restore"), "UNKNOWN")

    def test_partial_before_restore_and_pass_when_every_identity_was_denied(self):
        denied = {"source-dev": log(session("bravo-source-backup-writer")), "app-ns": log(session("bravo-source-workloads-app")),
                  "t35-restore-check": log(session("bravo-source-backup-reader"))}
        result = self.probe(logs=denied)
        self.assertEqual(verify.status(READBACK, result, "final"), "PASS")
        before = dict(result, reader="UNKNOWN_SA_NOT_FOUND")
        self.assertEqual(verify.status(READBACK, before, "before-restore"), "PARTIAL")
        self.assertEqual(verify.status(READBACK, before, "final"), "UNKNOWN")
        mismatch = dict(result, app="UNKNOWN_ROLE_MISMATCH")
        self.assertEqual(verify.status(READBACK, mismatch, "final"), "UNKNOWN")

    def test_deletion_or_wrong_configuration_fails(self):
        denied = {ns: log(session("x")) for ns in ("source-dev", "app-ns", "t35-restore-check")}
        result = self.probe(logs=denied, deleted=True)
        self.assertEqual(verify.status(READBACK, result, "final"), "FAIL")
        ok = {k: "DENIED" for k in SUBJECTS} | {"probe_version_intact": True, "no_delete_marker": True,
                                                  "custodian_removed_probe": True}
        self.assertEqual(verify.status(dict(READBACK, policy_matches_stack=False), ok, "final"), "FAIL")
        self.assertEqual(verify.status(READBACK, dict(ok, app="DELETED"), "final"), "FAIL")
        self.assertEqual(verify.status(READBACK, None, "final"), "UNKNOWN")
        self.assertEqual(verify.EXIT["PASS"], 0)
        self.assertNotEqual(verify.EXIT["PARTIAL"], 0)

    def test_extra_identity_needs_its_role(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            verify._identity("bravo-platform/bravo-executor")
        self.assertEqual(verify._identity(f"bravo-platform/bravo-executor={APP}"), ("bravo-platform", "bravo-executor", APP))


if __name__ == "__main__":
    unittest.main()
