"""A copy counts as done only when the Job, its one Pod and a well-formed receipt all agree."""
import ast
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

WORKLOADS = Path(os.environ.get("T8_ATTACH_MODULE_DIR")
                 or Path(__file__).resolve().parents[2] / "infra/envs/target-workloads")
with patch.object(sys, "path", [str(WORKLOADS), *sys.path]), patch.dict(sys.modules):
    for shadowed in ("render", "replication"):
        sys.modules.pop(shadowed, None)
    attachment_job = importlib.import_module("attachment_job")
    attachment_verdict = importlib.import_module("attachment_verdict")
    attachment_copy = importlib.import_module("attachment_copy")
    sync_attachments = importlib.import_module("sync_attachments")

DIGEST = "sha256:" + "b" * 64
IMAGE = "registry.example.invalid/bravo/attachment-copy@" + DIGEST
RUN_ID = "demo-2026-11-07-r1"
PLAN_ID = "Plan_2026-11-07"
EPOCH = 3
ADDRESS = "10.43.0.25"
SERVICE_UID = "0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f"
JOB_UID = "11111111-1111-4111-8111-111111111111"
POD_UID = "22222222-2222-4222-8222-222222222222"
STARTED = "2026-11-07T01:00:00Z"
FINISHED = "2026-11-07T01:00:09Z"
HASH_A, HASH_B = "a" * 64, "c" * 64
PRIVATE_KEY = "attachments/private-name"

SUCCESS = {"passed": True, "source_count": 2, "destination_count": 2, "database_referenced_count": 2,
           "operator_approved_extra_count": 0, "copied": 1, "reused": 1, "bytes_verified": 119,
           "key_content_inventory_sha256": "d" * 64, "content_sha256": [HASH_A, HASH_B]}
FAILURE = attachment_copy.AttachmentCopyFailed(
    "transfer", "source bucket and database attachment inventory differ", error_category="ValueError").receipt()


def job(condition="Complete", **status):
    rendered = attachment_job.job(IMAGE, RUN_ID, PLAN_ID, EPOCH, ADDRESS, SERVICE_UID)
    rendered["metadata"]["uid"] = JOB_UID
    conditions = [{"type": condition, "status": "True", "lastTransitionTime": FINISHED}] if condition else []
    counters = {"succeeded": 1} if condition == "Complete" else {"failed": 1} if condition == "Failed" else {"active": 1}
    rendered["status"] = {"startTime": STARTED, "conditions": conditions, **counters, **status}
    return rendered


def pod(exit_code=0):
    return {
        "metadata": {"name": "attachment-copy-x", "uid": POD_UID,
                     "ownerReferences": [{"kind": "Job", "name": "attachment-copy-" + RUN_ID, "uid": JOB_UID}]},
        "spec": {"containers": [{"name": "copy", "image": IMAGE}]},
        "status": {"containerStatuses": [{
            "name": "copy", "imageID": "registry.example.invalid/bravo/attachment-copy@" + DIGEST,
            "state": {"terminated": {"exitCode": exit_code}}}]},
    }


def log(receipt):
    return json.dumps(receipt) + "\n"


def judge(job_value=None, pods=None, log_text=None, run_id=RUN_ID, image=IMAGE):
    return attachment_verdict.judge(run_id, PLAN_ID, EPOCH, image, job() if job_value is None else job_value,
                                    [pod()] if pods is None else pods,
                                    log(SUCCESS) if log_text is None else log_text)


def failed_case():
    return {"job_value": job("Failed"), "pods": [pod(1)], "log_text": log(FAILURE)}


class Decisions(unittest.TestCase):
    def test_everything_agrees_is_success_and_binds_the_run(self):
        result = judge()
        self.assertEqual(result["verdict"], "SUCCEEDED")
        self.assertEqual(result["schema"], "bravo.t8.attachment-copy-result/v1")
        self.assertEqual((result["run_id"], result["job_uid"], result["pod_uid"]), (RUN_ID, JOB_UID, POD_UID))
        self.assertEqual((result["plan_id"], result["epoch"]), (PLAN_ID, EPOCH))
        self.assertEqual((result["started_at"], result["finished_at"]), (STARTED, FINISHED))
        self.assertEqual(result["image_digest"], DIGEST)
        self.assertEqual(result["receipt"], SUCCESS)
        self.assertEqual(result["container_exit_code"], 0)

    def test_a_reported_failure_is_failed_not_unknown(self):
        result = judge(**failed_case())
        self.assertEqual((result["verdict"], result["reason_code"]), ("FAILED", "receipt_failed"))
        self.assertEqual(result["receipt"], FAILURE)

    def test_real_success_receipt_from_the_copy_code_is_accepted(self):
        class Store:
            def __init__(self, values):
                self.values = values

            def list_keys(self):
                return list(self.values)

            def sizes(self):
                return {key: len(value) for key, value in self.values.items()}

            def read(self, key):
                return self.values[key]

            def create(self, key, value):
                self.values[key] = value

        receipt = attachment_copy.copy_keys(Store({"a": b"1", "b": b"2"}), Store({}), ["a", "b"])
        self.assertEqual(judge(log_text=log(receipt))["verdict"], "SUCCEEDED")

    def test_every_public_failure_reason_from_the_copy_code_is_recognized(self):
        reasons = [*attachment_copy.PUBLIC_REASONS, "unclassified", "missing ATTACHMENT_SOURCE_BUCKET",
                   "source and destination are the same bucket", "database connection must use verify-full TLS",
                   "database target must be one host from the DSN or PG* environment"]
        for reason in reasons:
            receipt = attachment_copy.AttachmentCopyFailed("transfer", reason).receipt()
            with self.subTest(reason=reason):
                self.assertEqual(judge(job("Failed"), [pod(1)], log(receipt))["verdict"], "FAILED")

    def test_every_failure_the_copy_code_can_raise_is_recognized(self):
        """A new reason in attachment_copy.py must be added here, not silently read as UNKNOWN."""
        tree = ast.parse((WORKLOADS / "attachment_copy.py").read_text())
        raised = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and getattr(node.func, "id", None) == "AttachmentCopyFailed"]
        literal = [node for node in raised if isinstance(node.args[1], ast.Constant)]
        self.assertGreaterEqual(len(literal), 3)
        for node in literal:
            receipt = attachment_copy.AttachmentCopyFailed(node.args[0].value, node.args[1].value).receipt()
            with self.subTest(reason=node.args[1].value):
                self.assertEqual(judge(job("Failed"), [pod(1)], log(receipt))["verdict"], "FAILED")
        stages = {node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                  and getattr(node.targets[0], "id", None) == "stage" for node in [node.value]}
        self.assertEqual(stages, attachment_verdict.FAILURE_STAGES)

    def test_real_sanitized_exception_is_recognized(self):
        receipt = attachment_copy.describe_failure("transfer", OSError(PRIVATE_KEY)).receipt()
        self.assertEqual(judge(job("Failed"), [pod(1)], log(receipt))["verdict"], "FAILED")


def mutated(function):
    value = job()
    function(value)
    return value


def container_of(value):
    return value["spec"]["template"]["spec"]["containers"][0]


def receipt_with(**changes):
    return log({**SUCCESS, **changes})


def receipt_without(field):
    return log({k: v for k, v in SUCCESS.items() if k != field})


def failure_with(**changes):
    return log({**FAILURE, **changes})


class NothingUncertainCountsAsSuccess(unittest.TestCase):
    def assert_unknown(self, reason, **case):
        result = judge(**case)
        self.assertEqual((result["verdict"], result["reason_code"]), ("UNKNOWN", reason))
        self.assertIsNone(result["receipt"])

    def test_job_cannot_be_read(self):
        result = attachment_verdict.judge(RUN_ID, PLAN_ID, EPOCH, IMAGE, None, None, None)
        self.assertEqual((result["verdict"], result["reason_code"]), ("UNKNOWN", "job_unreadable"))

    def test_job_is_not_the_one_this_run_rendered(self):
        cases = {
            "name": lambda j: j["metadata"].update(name="attachment-copy-other"),
            "namespace": lambda j: j["metadata"].update(namespace="default"),
            "run label": lambda j: j["metadata"]["labels"].update({attachment_job.RUN_LABEL: "other"}),
            "retries": lambda j: j["spec"].update(backoffLimit=1),
            "parallel": lambda j: j["spec"].update(parallelism=2),
            "completions": lambda j: j["spec"].update(completions=2),
            "image": lambda j: container_of(j).update(image="registry.example.invalid/other@" + DIGEST),
            "command": lambda j: container_of(j).update(command=["/bin/sh", "-c", "echo fabricated"]),
            "two containers": lambda j: j["spec"]["template"]["spec"]["containers"].append(container_of(j)),
            "stand-in source": lambda j: container_of(j)["env"].append(
                {"name": "ATTACHMENT_SOURCE_ENDPOINT_URL", "value": "http://stand-in.invalid"}),
            "other target": lambda j: next(e for e in container_of(j)["env"] if e["name"] == "ATTACHMENT_TARGET_BUCKET")
            .update(value="other-bucket"),
            "other password": lambda j: next(e for e in container_of(j)["env"] if e["name"] == "PGPASSWORD")
            ["valueFrom"]["secretKeyRef"].update(name="other"),
            "bad egress": lambda j: next(e for e in container_of(j)["env"] if e["name"] == "PGHOSTADDR")
            .update(value="8.8.8.8"),
            "no service uid": lambda j: j["spec"]["template"]["metadata"]["annotations"].pop(
                attachment_job.EGRESS_UID_ANNOTATION),
            "other plan": lambda j: j["spec"]["template"]["metadata"]["annotations"].update(
                {attachment_job.PLAN_ANNOTATION: "other-plan"}),
            "other epoch": lambda j: j["spec"]["template"]["metadata"]["annotations"].update(
                {attachment_job.EPOCH_ANNOTATION: str(EPOCH + 1)}),
            "no job uid": lambda j: j["metadata"].pop("uid"),
            "odd job uid": lambda j: j["metadata"].update(uid=PRIVATE_KEY),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assert_unknown("job_not_as_rendered", job_value=mutated(change))

    def test_approved_extras_job_is_recognized_as_rendered(self):
        rendered = attachment_job.job(IMAGE, RUN_ID, PLAN_ID, EPOCH, ADDRESS, SERVICE_UID, with_approved_extras=True)
        rendered["metadata"]["uid"] = JOB_UID
        rendered["status"] = job()["status"]
        self.assertEqual(judge(job_value=rendered)["verdict"], "SUCCEEDED")

    def test_job_is_still_running(self):
        self.assert_unknown("job_not_finished", job_value=job(None))

    def test_condition_present_but_not_true_is_not_finished(self):
        value = job()
        value["status"]["conditions"][0]["status"] = "False"
        self.assert_unknown("job_not_finished", job_value=value)

    def test_both_terminal_conditions_conflict(self):
        value = job()
        value["status"]["conditions"].append({"type": "Failed", "status": "True"})
        self.assert_unknown("job_conditions_conflict", job_value=value)

    def test_pod_was_cleaned_up_or_duplicated(self):
        self.assert_unknown("pod_count_not_one", pods=[])
        self.assert_unknown("pod_count_not_one", pods=[pod(), pod()])

    def test_pod_belongs_to_another_job(self):
        other = pod()
        other["metadata"]["ownerReferences"][0]["uid"] = "abcdef12-3456-4789-8abc-def123456789"
        self.assert_unknown("pod_not_as_rendered", pods=[other])

    def test_pod_runs_another_image(self):
        other = pod()
        other["spec"]["containers"][0]["image"] = "registry.example.invalid/other@" + DIGEST
        self.assert_unknown("pod_not_as_rendered", pods=[other])

    def test_container_never_terminated(self):
        running = pod()
        running["status"]["containerStatuses"][0]["state"] = {"running": {}}
        self.assert_unknown("pod_not_terminated", pods=[running])
        missing = pod()
        missing["status"] = {}
        self.assert_unknown("pod_not_terminated", pods=[missing])

    def test_runtime_image_is_not_the_pinned_digest(self):
        other = pod()
        other["status"]["containerStatuses"][0]["imageID"] = "registry.example.invalid/x@sha256:" + "e" * 64
        self.assert_unknown("image_digest_mismatch", pods=[other])

    def test_log_cannot_be_read(self):
        self.assertEqual(attachment_verdict.judge(RUN_ID, PLAN_ID, EPOCH, IMAGE, job(), [pod()], None)["reason_code"],
                         "receipt_unreadable")
        self.assert_unknown("receipt_unreadable", log_text="\n")

    def test_log_is_not_exactly_one_receipt(self):
        for text in ("not json\n", log(SUCCESS) * 2, log(SUCCESS) + "warning\n", "[1]\n", "null\n", '"text"\n'):
            with self.subTest(text=text):
                self.assert_unknown("receipt_invalid", log_text=text)

    def test_success_receipt_shape_is_exact(self):
        cases = [receipt_with(extra=1), receipt_with(passed="true"), receipt_with(passed=1),
                 receipt_with(source_write_calls=0), *[receipt_without(field) for field in SUCCESS]]
        for text in cases:
            with self.subTest(text=text):
                self.assert_unknown("receipt_invalid", log_text=text)

    def test_success_receipt_numbers_must_add_up(self):
        cases = [
            receipt_with(destination_count=1),
            receipt_with(database_referenced_count=1),
            receipt_with(operator_approved_extra_count=1),
            receipt_with(copied=2),
            receipt_with(reused=0),
            receipt_with(source_count=True, destination_count=True, database_referenced_count=True,
                         copied=1, reused=0, content_sha256=[HASH_A]),
            receipt_with(copied=-1, reused=3),
            receipt_with(bytes_verified=-1),
            receipt_with(bytes_verified=True),
            receipt_with(bytes_verified=1.5),
            receipt_with(bytes_verified=sync_attachments.MAX_BYTES + 1),
            log({**SUCCESS, "source_count": 0, "destination_count": 0, "database_referenced_count": 0,
                 "copied": 0, "reused": 0, "content_sha256": []}),
            log({**SUCCESS, "source_count": 101, "destination_count": 101, "database_referenced_count": 101,
                 "copied": 101, "reused": 0, "content_sha256": [HASH_A] * 101}),
        ]
        for text in cases:
            with self.subTest(text=text[:200]):
                self.assert_unknown("receipt_invalid", log_text=text)

    def test_success_receipt_hashes_must_be_well_formed(self):
        cases = [receipt_with(key_content_inventory_sha256="zz"),
                 receipt_with(key_content_inventory_sha256="D" * 64),
                 receipt_with(content_sha256=[HASH_A]),
                 receipt_with(content_sha256=[HASH_B, HASH_A]),
                 receipt_with(content_sha256=[HASH_A, "x"]),
                 receipt_with(content_sha256=HASH_A + HASH_B),
                 receipt_with(content_sha256=[HASH_A, None])]
        for text in cases:
            with self.subTest(text=text):
                self.assert_unknown("receipt_invalid", log_text=text)

    def test_failure_receipt_shape_is_closed(self):
        cases = [failure_with(reason=f"could not read {PRIVATE_KEY}"), failure_with(stage="elsewhere"),
                 failure_with(s3_error_code="SomethingNew"), failure_with(error_category="has spaces in it"),
                 failure_with(raw_output_suppressed=False), failure_with(extra=1), failure_with(passed=0)]
        for text in cases:
            with self.subTest(text=text):
                self.assert_unknown("receipt_invalid", job_value=job("Failed"), pods=[pod(1)], log_text=text)

    def test_job_and_receipt_must_tell_the_same_story(self):
        disagree = "job_and_receipt_disagree"
        self.assert_unknown(disagree, log_text=log(FAILURE))
        self.assert_unknown(disagree, job_value=job("Failed"), pods=[pod(1)], log_text=log(SUCCESS))
        self.assert_unknown(disagree, pods=[pod(1)])
        self.assert_unknown(disagree, job_value=job("Failed"), pods=[pod(137)], log_text=log(FAILURE))
        self.assert_unknown(disagree, job_value=job(succeeded=0))
        self.assert_unknown(disagree, job_value=job(failed=1))
        self.assert_unknown(disagree, job_value=job("Failed", succeeded=1), pods=[pod(1)], log_text=log(FAILURE))

    def test_each_side_of_the_story_is_required_on_its_own(self):
        disagree = "job_and_receipt_disagree"
        # Everything else says success, but the Job's condition says it failed.
        self.assert_unknown(disagree, job_value=job("Failed", succeeded=1, failed=0))
        # Everything else says a reported failure, but the Job's condition says it completed.
        self.assert_unknown(disagree, job_value=job(succeeded=0, failed=1), pods=[pod(1)], log_text=log(FAILURE))
        # The Job failed, yet it counts no failed Pod behind the receipt.
        self.assert_unknown(disagree, job_value=job("Failed", failed=0), pods=[pod(1)], log_text=log(FAILURE))

    def test_killed_job_without_a_receipt_is_unknown_and_keeps_the_cause(self):
        value = job("Failed")
        value["status"]["conditions"][0]["reason"] = "DeadlineExceeded"
        result = judge(job_value=value, pods=[pod(137)], log_text="\n")
        self.assertEqual((result["verdict"], result["reason_code"]), ("UNKNOWN", "receipt_unreadable"))
        self.assertEqual(result["job_failure_reason"], "DeadlineExceeded")
        self.assertEqual(result["container_exit_code"], 137)

    def test_deadline_that_removed_the_pod_is_unknown(self):
        value = job("Failed")
        value["status"]["conditions"][0]["reason"] = "DeadlineExceeded"
        result = judge(job_value=value, pods=[])
        self.assertEqual((result["verdict"], result["job_failure_reason"]), ("UNKNOWN", "DeadlineExceeded"))

    def test_unexpected_condition_reason_is_not_repeated(self):
        value = job("Failed")
        value["status"]["conditions"][0]["reason"] = PRIVATE_KEY
        self.assertEqual(judge(job_value=value, pods=[pod(1)], log_text=log(FAILURE))["job_failure_reason"],
                         "unclassified")

    def test_malformed_objects_never_raise_or_pass(self):
        for value in ({}, [], "job", {"metadata": None}, {"metadata": {}, "spec": []},
                      {"metadata": {"name": "attachment-copy-" + RUN_ID, "namespace": "target-dev"},
                       "spec": {"template": None}}):
            with self.subTest(value=value):
                result = attachment_verdict.judge(RUN_ID, PLAN_ID, EPOCH, IMAGE, value, [pod()], log(SUCCESS))
                self.assertEqual(result["verdict"], "UNKNOWN")
        malformed_pods = ([None], ["pod"], [{}], [{"metadata": None}],
                          [{"metadata": {"ownerReferences": [None]}}], [{"metadata": {"ownerReferences": "x"}}],
                          "not-a-list")
        for pods in malformed_pods:
            with self.subTest(pods=pods):
                result = attachment_verdict.judge(RUN_ID, PLAN_ID, EPOCH, IMAGE, job(), pods, log(SUCCESS))
                self.assertEqual(result["verdict"], "UNKNOWN")

    def test_timestamps_of_a_job_with_unusual_status_stay_out_of_the_decision(self):
        value = job()
        del value["status"]["startTime"]
        self.assertEqual(judge(job_value=value)["started_at"], None)

    def test_timestamp_fields_only_repeat_timestamps(self):
        value = job()
        value["status"]["startTime"] = PRIVATE_KEY
        value["status"]["conditions"][0]["lastTransitionTime"] = PRIVATE_KEY
        result = judge(job_value=value)
        self.assertEqual((result["started_at"], result["finished_at"]), (None, None))

    def test_raw_log_text_is_never_repeated(self):
        for text in (f"{PRIVATE_KEY}\n", failure_with(reason=PRIVATE_KEY), receipt_with(extra=PRIVATE_KEY)):
            with self.subTest(text=text):
                self.assertNotIn(PRIVATE_KEY, json.dumps(judge(log_text=text)))


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Cluster:
    """Answers the three read-only kubectl questions and records them."""

    def __init__(self, job_value=None, pods=None, log_text=None):
        self.answers = {"job": completed(json.dumps(job() if job_value is None else job_value)),
                        "pods": completed(json.dumps({"items": [pod()] if pods is None else pods})),
                        "logs": completed(log(SUCCESS) if log_text is None else log_text)}
        self.commands = []

    def __call__(self, command, **options):
        self.commands.append((command, options))
        if " get job " in " ".join(command):
            answer = self.answers["job"]
        elif " get pods " in " ".join(command):
            answer = self.answers["pods"]
        else:
            answer = self.answers["logs"]
        if isinstance(answer, Exception):
            raise answer
        return answer


class ReadingTheCluster(unittest.TestCase):
    def read(self, cluster, kubeconfig=None):
        with patch.object(attachment_verdict.subprocess, "run", cluster):
            return attachment_verdict.read_and_judge(RUN_ID, PLAN_ID, EPOCH, IMAGE, kubeconfig)

    def test_success_through_the_reader(self):
        self.assertEqual(self.read(Cluster())["verdict"], "SUCCEEDED")

    def test_only_read_verbs_with_timeouts_and_a_bounded_log(self):
        cluster = Cluster()
        self.read(cluster, "/private/kubeconfig")
        for command, options in cluster.commands:
            self.assertEqual(command[:3], ["kubectl", "--kubeconfig", "/private/kubeconfig"])
            self.assertEqual(command[3:5], ["-n", "target-dev"])
            self.assertIn(command[5], ("get", "logs"))
            self.assertGreater(options["timeout"], 0)
            self.assertIs(options["check"], False)
            self.assertIs(options["capture_output"], True)
        verbs = [" ".join(command[5:]) for command, _ in cluster.commands]
        self.assertEqual(verbs[0], f"get job attachment-copy-{RUN_ID} -o json")
        self.assertEqual(verbs[1], f"get pods -l batch.kubernetes.io/job-name=attachment-copy-{RUN_ID} -o json")
        self.assertEqual(verbs[2], "logs pod/attachment-copy-x -c copy "
                                   f"--limit-bytes={attachment_verdict.LOG_LIMIT_BYTES}")

    def test_kubeconfig_is_optional(self):
        cluster = Cluster()
        self.read(cluster)
        self.assertEqual(cluster.commands[0][0][:3], ["kubectl", "-n", "target-dev"])

    def test_bounded_log_holds_the_largest_receipt(self):
        largest = {**SUCCESS, "source_count": 100, "destination_count": 100, "database_referenced_count": 100,
                   "copied": 100, "reused": 0, "bytes_verified": sync_attachments.MAX_BYTES,
                   "content_sha256": [HASH_A] * 100}
        self.assertLess(len(log(largest)), attachment_verdict.LOG_LIMIT_BYTES)

    def test_truncated_log_is_not_a_receipt(self):
        text = log(SUCCESS)[:40]
        self.assertEqual(self.read(Cluster(log_text=text))["reason_code"], "receipt_invalid")

    def test_no_command_failure_counts_as_success_and_stderr_is_not_repeated(self):
        for part, reason in (("job", "job_unreadable"), ("pods", "pod_count_not_one"), ("logs", "receipt_unreadable")):
            for failure in (completed(returncode=1, stderr=PRIVATE_KEY), subprocess.TimeoutExpired("kubectl", 1),
                            OSError(PRIVATE_KEY), completed("not json")):
                if part == "logs" and failure.__class__ is subprocess.CompletedProcess and failure.returncode == 0:
                    continue
                cluster = Cluster()
                cluster.answers[part] = failure
                with self.subTest(part=part, failure=failure):
                    result = self.read(cluster)
                    self.assertEqual((result["verdict"], result["reason_code"]), ("UNKNOWN", reason))
                    self.assertNotIn(PRIVATE_KEY, json.dumps(result))

    def test_output_of_a_failed_command_is_not_trusted(self):
        cluster = Cluster()
        cluster.answers["logs"] = completed(log(SUCCESS), returncode=1)
        self.assertEqual(self.read(cluster)["reason_code"], "receipt_unreadable")

    def test_no_log_is_requested_when_the_pod_is_gone(self):
        cluster = Cluster(pods=[])
        self.assertEqual(self.read(cluster)["reason_code"], "pod_count_not_one")
        self.assertEqual(len(cluster.commands), 2)

    def test_a_pod_name_that_is_not_a_kubernetes_name_is_not_passed_to_kubectl(self):
        odd = pod()
        odd["metadata"]["name"] = "--namespace=other"
        cluster = Cluster(pods=[odd])
        self.assertEqual(self.read(cluster)["verdict"], "UNKNOWN")
        self.assertEqual(len(cluster.commands), 2)


class CommandLine(unittest.TestCase):
    def run_cli(self, cluster, *extra):
        output = io.StringIO()
        arguments = ["attachment_verdict.py", "--run-id", RUN_ID, "--plan-id", PLAN_ID, "--epoch", str(EPOCH),
                     "--image", IMAGE, *extra]
        with patch.object(attachment_verdict.subprocess, "run", cluster), patch.object(sys, "argv", arguments), \
                contextlib.redirect_stdout(output):
            code = attachment_verdict.main()
        return code, output.getvalue()

    def test_exit_codes_separate_the_three_outcomes(self):
        for cluster, expected_code, verdict in (
                (Cluster(), 0, "SUCCEEDED"),
                (Cluster(job("Failed"), [pod(1)], log(FAILURE)), 1, "FAILED"),
                (Cluster(job(None)), 2, "UNKNOWN")):
            with self.subTest(verdict=verdict):
                code, output = self.run_cli(cluster)
                self.assertEqual(code, expected_code)
                self.assertEqual(json.loads(output)["verdict"], verdict)
                self.assertEqual(output.count("\n"), 1)

    def test_invalid_inputs_are_refused_before_reading(self):
        plan = ["--plan-id", PLAN_ID, "--epoch", str(EPOCH)]
        for arguments in (["--run-id", "BAD", *plan, "--image", IMAGE], ["--run-id", RUN_ID, *plan, "--image", "x:latest"],
                          ["--run-id", RUN_ID, "--plan-id", "bad plan", "--epoch", "1", "--image", IMAGE],
                          ["--run-id", RUN_ID, "--plan-id", PLAN_ID, "--epoch", "0", "--image", IMAGE]):
            cluster = Cluster()
            with self.subTest(arguments=arguments), patch.object(attachment_verdict.subprocess, "run", cluster), \
                    patch.object(sys, "argv", ["attachment_verdict.py", *arguments]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                attachment_verdict.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(cluster.commands, [])


if __name__ == "__main__":
    unittest.main()
