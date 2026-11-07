"""T35 source backup verification: a policy that does not block deletion and a probe that did not run are never a pass."""
import argparse
import importlib.util
import io
import json
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from string import Template
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("source_backup_verify", ROOT / "infra/envs/source-backup/verify.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

BUCKET = "bravo-source-backup-111122223333"
ARN = Template("arn:aws:s3:::${bucket}").substitute(bucket=BUCKET)
CUSTODIANS = ["arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_Admin_0123456789abcdef"]
WRITER = "arn:aws:iam::111122223333:role/bravo-source-backup-writer"
READER = "arn:aws:iam::111122223333:role/bravo-source-backup-reader"
APP = "arn:aws:iam::111122223333:role/bravo-source-workloads-app"
EXECUTOR = "arn:aws:iam::111122223333:role/bravo-platform-node"
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
            lambda p: p["Statement"][1].update(Resource=Template("arn:aws:s3:::${bucket}/*").substitute(bucket="other-bucket")),
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


# Real aws-cli 2.36.15 output with no credential source at all (exit 253)
NO_CREDENTIALS = ('aws: [ERROR]: An error occurred (NoCredentials): Unable to locate credentials. '
                  'You can configure credentials by running "aws login".')
NODE = "ip-10-0-1-10.ap-northeast-2.compute.internal"
CLOSED = {"HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 1}


def executor_pod(node=NODE, env=(), phase="Running", **spec):
    """A running Executor Pod as the API returns it: only the API token is projected, no AWS source."""
    container = {"name": "executor", "env": [{"name": "AWS_REGION", "value": "ap-northeast-2"}, *env],
                 "volumeMounts": [{"name": "kube-api-access", "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount"}]}
    return {"metadata": {"name": "bravo-executor-7d9f"}, "status": {"phase": phase},
            "spec": {"serviceAccountName": "bravo-executor", "nodeName": node, "containers": [container],
                     "volumes": [{"name": "kube-api-access", "projected": {"sources": [
                         {"serviceAccountToken": {"expirationSeconds": 3607, "path": "token"}}]}}], **spec}}


def fake(sa_stderr=None, logs=None, deleted=False, pods=None, provider="aws:///ap-northeast-2a/i-0123456789abcdef0",
         metadata=None, instances=1):
    """Fake aws/kubectl for probe(): ServiceAccount lookup result and per-namespace Pod logs.

    pods/provider/metadata/instances answer the --executor-without-aws reads; create.created keeps the Pods made."""
    created = {}

    def res(out="", code=0, err=""):
        return types.SimpleNamespace(returncode=code, stdout=out if isinstance(out, str) else json.dumps(out), stderr=err)

    def run(cmd, check=True):
        c = " ".join(cmd)
        if "put-object" in c:
            return res({"VersionId": "v1"})
        if "get serviceaccount" in c:
            return res("", 1, sa_stderr) if sa_stderr else res("serviceaccount found")
        if "get pods" in c:
            return res({"items": pods if pods is not None else [executor_pod()]})
        if "get node" in c:
            return res(provider)
        if "describe-instances" in c:
            return res({"Reservations": [{"Instances": [{"MetadataOptions": metadata or CLOSED}] * instances}]})
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
    create.created = created
    return run, create


REG = {"bucket": BUCKET, "region": "ap-northeast-2", "destination": f"s3://{BUCKET}/t35/source-db/",
       "writer": {"namespace": "source-dev", "name": "source-db"}, "reader": {"namespace": "t35-restore-check", "name": "source-db-restore"},
       "writer_role_arn": WRITER, "reader_role_arn": READER}
SUBJECTS = {"writer": ("source-dev", "source-db", WRITER), "reader": ("t35-restore-check", "source-db-restore", READER),
            "app": ("app-ns", "demo-chat", APP), "executor": ("bravo-platform", "bravo-executor", EXECUTOR)}
READBACK = {"versioning_enabled": True, "policy_matches_stack": True}


class Probe(unittest.TestCase):
    def probe(self, subjects=SUBJECTS, **kw):
        run, create = fake(**kw)
        self.created = create.created
        with patch.object(verify, "run", run), patch.object(verify.subprocess, "run", create), patch.object(verify.time, "sleep"):
            return verify.probe(["aws"], ["kubectl"], REG, subjects)

    def test_forbidden_lookups_are_unknown_not_pass(self):
        # Review reproduction: every ServiceAccount lookup Forbidden used to pass with exit 0
        result = self.probe(sa_stderr='Error from server (Forbidden): serviceaccounts "x" is forbidden')
        self.assertEqual({result[k] for k in SUBJECTS}, {"UNKNOWN_SA_LOOKUP_FAILED"})
        self.assertEqual(verify.status(READBACK, result, "final"), "UNKNOWN")
        self.assertEqual(verify.status(READBACK, result, "before-restore"), "UNKNOWN")

    def test_partial_before_restore_and_pass_when_every_identity_was_denied(self):
        denied = {"source-dev": log(session("bravo-source-backup-writer")), "app-ns": log(session("bravo-source-workloads-app")),
                  "t35-restore-check": log(session("bravo-source-backup-reader")), "bravo-platform": log(session("bravo-platform-node"))}
        result = self.probe(logs=denied)
        self.assertEqual(verify.status(READBACK, result, "final"), "PASS")
        before = dict(result, reader="UNKNOWN_SA_NOT_FOUND")
        self.assertEqual(verify.status(READBACK, before, "before-restore"), "PARTIAL")
        self.assertEqual(verify.status(READBACK, before, "final"), "UNKNOWN")
        mismatch = dict(result, app="UNKNOWN_ROLE_MISMATCH")
        self.assertEqual(verify.status(READBACK, mismatch, "final"), "UNKNOWN")

    def test_deletion_or_wrong_configuration_fails(self):
        denied = {ns: log(session("x")) for ns in ("source-dev", "app-ns", "t35-restore-check", "bravo-platform")}
        result = self.probe(logs=denied, deleted=True)
        self.assertEqual(verify.status(READBACK, result, "final"), "FAIL")
        ok = {k: "DENIED" for k in SUBJECTS} | {"probe_version_intact": True, "no_delete_marker": True,
                                                  "custodian_removed_probe": True}
        self.assertEqual(verify.status(dict(READBACK, policy_matches_stack=False), ok, "final"), "FAIL")
        self.assertEqual(verify.status(READBACK, dict(ok, app="DELETED"), "final"), "FAIL")
        self.assertEqual(verify.status(READBACK, None, "final"), "UNKNOWN")
        self.assertEqual(verify.EXIT["PASS"], 0)
        self.assertNotEqual(verify.EXIT["PARTIAL"], 0)

    def test_final_pass_needs_the_executor(self):
        # Review reproduction: main() with only --app-role-arn returned PASS without ever probing an Executor
        verified = {"writer": "DENIED", "reader": "DENIED", "app": "DENIED", "probe_version_intact": True,
                    "no_delete_marker": True, "custodian_removed_probe": True}
        calls = []

        def fake_probe(aws, kube, reg, subjects):
            calls.append(set(subjects))
            return dict(verified, **({"executor": "DENIED"} if "executor" in subjects else {}))

        args = ["--registration", "-", "--kubeconfig", "k", "--delete-probe", "--stage", "final", "--app-role-arn", APP]
        for extra, expected in (([], 2), (["--executor", f"bravo-platform/bravo-executor={EXECUTOR}"], 0)):
            with patch.object(verify, "readback", lambda aws, reg: dict(READBACK)), patch.object(verify, "probe", fake_probe), \
                    patch.object(verify.sys, "stdin", io.StringIO(json.dumps(REG))), redirect_stdout(io.StringIO()) as out:
                code = verify.main(args + extra)
            self.assertEqual(code, expected, out.getvalue())
            if not extra:  # the report names the missing identity instead of omitting it
                self.assertEqual(json.loads(out.getvalue())["delete_probe"]["executor"], "UNKNOWN_NOT_REGISTERED")
        self.assertNotIn("executor", calls[0])
        self.assertIn("executor", calls[1])
        self.assertEqual(verify.status(READBACK, dict(verified, executor="UNKNOWN_NOT_REGISTERED"), "before-restore"), "UNKNOWN")
        # A result that simply lacks the Executor is not a pass either
        self.assertEqual(verify.status(READBACK, verified, "final"), "UNKNOWN")
        self.assertEqual(verify.status(READBACK, dict(verified, reader="UNKNOWN_SA_NOT_FOUND"), "before-restore"), "UNKNOWN")

    def test_extra_identity_needs_its_role(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            verify._identity("bravo-platform/bravo-executor")
        self.assertEqual(verify._identity(f"bravo-platform/bravo-executor={APP}"), ("bravo-platform", "bravo-executor", APP))


NO_AWS = dict(SUBJECTS, executor=("bravo-platform", "bravo-executor", None))
DENIED_LOGS = {"source-dev": log(session("bravo-source-backup-writer")), "app-ns": log(session("bravo-source-workloads-app")),
               "t35-restore-check": log(session("bravo-source-backup-reader"))}


def no_credentials(principal=""):
    return log(principal, 253, 253, (NO_CREDENTIALS, NO_CREDENTIALS))


class ExecutorWithoutAws(unittest.TestCase):
    """#57 (11/6): the source Executor has no AWS identity, so --executor reported UNKNOWN_NO_CREDENTIALS forever."""

    def probe(self, executor_log=None, **kw):
        logs = dict(DENIED_LOGS, **{"bravo-platform": executor_log if executor_log is not None else no_credentials()})
        run, create = fake(logs=logs, **kw)
        self.created = create.created
        with patch.object(verify, "run", run), patch.object(verify.subprocess, "run", create), patch.object(verify.time, "sleep"):
            return verify.probe(["aws"], ["kubectl"], REG, NO_AWS)

    def test_no_credentials_twice_on_the_executor_node_is_a_refusal(self):
        result = self.probe()
        self.assertEqual(result["executor"], "NO_AWS_IDENTITY")
        self.assertEqual(self.created["bravo-platform"]["spec"]["nodeName"], NODE)  # pinned to the Executor's node
        self.assertEqual(self.created["bravo-platform"]["spec"]["serviceAccountName"], "bravo-executor")
        self.assertNotIn("nodeName", self.created["source-dev"]["spec"])  # role identities stay scheduled as before
        self.assertEqual(verify.status(READBACK, result, "final"), "PASS")
        self.assertEqual(verify.status(READBACK, dict(result, reader="UNKNOWN_SA_NOT_FOUND"), "before-restore"), "PARTIAL")

    def test_the_probe_log_must_show_missing_credentials_for_both_deletes(self):
        self.assertEqual(verify.no_identity_verdict(no_credentials("None")), "NO_AWS_IDENTITY")
        cases = {
            no_credentials(session("bravo-platform-node")): "UNKNOWN_HAS_AWS_IDENTITY",  # metadata answered after all
            log("", 254, 254): "UNKNOWN_UNCLEAR",  # AccessDenied without a principal: not "no credentials"
            log("", 253, 255, (NO_CREDENTIALS, "Could not connect to the endpoint URL")): "UNKNOWN_UNCLEAR",
            log("", 0, 253, ("", NO_CREDENTIALS)): "DELETED",
            "principal=\n": "UNKNOWN_UNCLEAR",
        }
        for text, expected in cases.items():
            self.assertEqual(verify.no_identity_verdict(text), expected, text)
        result = self.probe(executor_log=no_credentials(session("bravo-platform-node")))
        self.assertEqual(verify.status(READBACK, result, "final"), "UNKNOWN")
        result = self.probe(executor_log=log("", 0, 0, ("", "")))
        self.assertEqual(verify.status(READBACK, result, "final"), "FAIL")

    def test_a_credential_source_in_the_running_pod_is_unknown_before_any_probe(self):
        sources = [
            executor_pod(env=[{"name": "AWS_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]),
            executor_pod(env=[{"name": "AWS_ROLE_ARN", "value": EXECUTOR}]),  # IRSA injection
            executor_pod(env=[{"name": "AWS_CONTAINER_CREDENTIALS_FULL_URI", "value": "http://169.254.170.23/v1/credentials"}]),
            executor_pod(hostNetwork=True),
            executor_pod(initContainers=[{"name": "init", "envFrom": [{"secretRef": {"name": "cloud"}}]}]),
            executor_pod(volumes=[{"name": "aws-iam-token", "projected": {"sources": [
                {"serviceAccountToken": {"audience": "sts.amazonaws.com", "path": "token"}}]}}]),
        ]
        mounted = executor_pod()
        mounted["spec"]["containers"][0]["volumeMounts"].append({"name": "cfg", "mountPath": "/var/lib/executor/.aws"})
        for pod in sources + [mounted]:
            self.assertTrue(verify.aws_sources(pod["spec"]), pod["spec"])
            result = self.probe(pods=[executor_pod(node="ip-10-0-2-20"), pod])
            self.assertEqual(result["executor"], "UNKNOWN_AWS_SOURCE_IN_POD")
            self.assertNotIn("bravo-platform", self.created)
        self.assertEqual(verify.aws_sources(executor_pod()["spec"]), [])  # AWS_REGION and the API token are not sources

    def test_the_executor_must_be_running_on_nodes_with_closed_metadata(self):
        self.assertEqual(self.probe(pods=[])["executor"], "UNKNOWN_NO_RUNNING_POD")
        self.assertEqual(self.probe(pods=[executor_pod(phase="Pending")])["executor"], "UNKNOWN_NO_RUNNING_POD")
        for kw in ({"metadata": {"HttpEndpoint": "enabled", "HttpTokens": "optional", "HttpPutResponseHopLimit": 1}},
                   {"metadata": {"HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 2}},
                   {"provider": "kind://docker/kind/node"}, {"instances": 2}):
            self.assertEqual(self.probe(**kw)["executor"], "UNKNOWN_NODE_METADATA", kw)
            self.assertNotIn("bravo-platform", self.created)
        self.assertEqual(self.probe(metadata={"HttpEndpoint": "disabled"})["executor"], "NO_AWS_IDENTITY")

    def test_only_the_executor_may_be_refused_by_having_no_identity(self):
        ok = {k: "DENIED" for k in SUBJECTS} | {"executor": "NO_AWS_IDENTITY", "probe_version_intact": True,
                                                  "no_delete_marker": True, "custodian_removed_probe": True}
        self.assertEqual(verify.status(READBACK, ok, "final"), "PASS")
        self.assertEqual(verify.status(READBACK, dict(ok, app="NO_AWS_IDENTITY"), "final"), "UNKNOWN")
        self.assertEqual(verify.status(READBACK, dict(ok, writer="NO_AWS_IDENTITY"), "final"), "UNKNOWN")

    def test_cli_registers_the_executor_without_a_role(self):
        self.assertEqual(verify._without_aws("bravo-platform/bravo-executor"), ("bravo-platform", "bravo-executor", None))
        for bad in ("bravo-executor", f"bravo-platform/bravo-executor={EXECUTOR}", "/bravo-executor", "a/b/c"):
            with self.assertRaises(argparse.ArgumentTypeError):
                verify._without_aws(bad)
        seen = []

        def fake_probe(aws, kube, reg, subjects):
            seen.append(subjects["executor"])
            return {"writer": "DENIED", "reader": "DENIED", "app": "DENIED", "executor": "NO_AWS_IDENTITY",
                    "probe_version_intact": True, "no_delete_marker": True, "custodian_removed_probe": True}

        args = ["--registration", "-", "--kubeconfig", "k", "--delete-probe", "--app-role-arn", APP,
                "--executor-without-aws", "bravo-platform/bravo-executor"]
        with patch.object(verify, "readback", lambda aws, reg: dict(READBACK)), patch.object(verify, "probe", fake_probe), \
                patch.object(verify.sys, "stdin", io.StringIO(json.dumps(REG))), redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(args), 0)
        self.assertEqual(seen, [("bravo-platform", "bravo-executor", None)])
        with patch.object(verify.sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
            verify.main(args + ["--executor", f"bravo-platform/bravo-executor={EXECUTOR}"])  # one or the other


if __name__ == "__main__":
    unittest.main()
