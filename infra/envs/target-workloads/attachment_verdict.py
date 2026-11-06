#!/usr/bin/env python3
"""Read one attachment copy run and decide SUCCEEDED, FAILED or UNKNOWN. Cluster access is read-only.

SUCCEEDED needs the rendered Job, its one Pod, the pinned image and a well-formed receipt to agree.
FAILED means the copy code itself reported a sanitized failure. Everything else is UNKNOWN: a
missing log, a cleaned-up Pod, a killed container or any disagreement. Only SUCCEEDED lets a
cutover continue. Raw log text and kubectl errors are never repeated because they can carry keys
or hosts. Exit codes: 0 SUCCEEDED, 1 FAILED, 2 UNKNOWN.
"""
import argparse
import json
import re
import subprocess

import attachment_job
from attachment_copy import PUBLIC_REASONS, PUBLIC_S3_CODES
from render import NAMESPACE
from sync_attachments import MAX_BYTES, MAX_OBJECTS

SCHEMA = "bravo.t8.attachment-copy-result/v1"
SUCCEEDED, FAILED, UNKNOWN = "SUCCEEDED", "FAILED", "UNKNOWN"
EXIT_CODES = {SUCCEEDED: 0, FAILED: 1, UNKNOWN: 2}
# The largest receipt is about 7KiB. A longer log gets cut, fails to parse and reads as UNKNOWN.
LOG_LIMIT_BYTES = 16384
KUBECTL_TIMEOUT_SECONDS = 30

SHA256 = re.compile(r"[0-9a-f]{64}")
KUBERNETES_UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
POD_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
EXCEPTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
MISSING_SETTING = re.compile(r"missing ATTACHMENT_[A-Z_]+")

COUNT_FIELDS = frozenset({"source_count", "destination_count", "database_referenced_count",
                          "operator_approved_extra_count", "copied", "reused", "bytes_verified"})
SUCCESS_FIELDS = COUNT_FIELDS | {"passed", "key_content_inventory_sha256", "content_sha256"}
FAILURE_FIELDS = frozenset({"passed", "stage", "reason", "error_category", "s3_error_code",
                            "raw_output_suppressed"})
FAILURE_STAGES = frozenset({"configuration", "database_inventory", "transfer"})
# Fixed strings raised by attachment_copy.py; anything else is treated as possibly leaked text.
FAILURE_REASONS = PUBLIC_REASONS | {
    "unclassified",
    "source and destination are the same bucket",
    "database connection must use verify-full TLS",
    "database target must be one host from the DSN or PG* environment",
}
JOB_FAILURE_REASONS = frozenset({"DeadlineExceeded", "BackoffLimitExceeded", "PodFailurePolicy"})


def _dig(value, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _is_sha256(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _matching(pattern, value):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _is_success_receipt(receipt):
    if set(receipt) != SUCCESS_FIELDS or receipt["passed"] is not True:
        return False
    if not all(type(receipt[field]) is int and receipt[field] >= 0 for field in COUNT_FIELDS):
        return False
    total = receipt["source_count"]
    hashes = receipt["content_sha256"]
    return (1 <= total <= MAX_OBJECTS and receipt["destination_count"] == total
            and receipt["database_referenced_count"] + receipt["operator_approved_extra_count"] == total
            and receipt["copied"] + receipt["reused"] == total
            and receipt["bytes_verified"] <= MAX_BYTES
            and _is_sha256(receipt["key_content_inventory_sha256"])
            and isinstance(hashes, list) and len(hashes) == total
            and all(_is_sha256(value) for value in hashes) and hashes == sorted(hashes))


def _is_failure_receipt(receipt):
    if set(receipt) != FAILURE_FIELDS or receipt["passed"] is not False or receipt["raw_output_suppressed"] is not True:
        return False
    reason, category, code = receipt["reason"], receipt["error_category"], receipt["s3_error_code"]
    return (receipt["stage"] in FAILURE_STAGES
            and isinstance(reason, str) and (reason in FAILURE_REASONS or MISSING_SETTING.fullmatch(reason) is not None)
            and (category is None or _matching(EXCEPTION_NAME, category) is not None)
            and (code is None or code == "unclassified" or code in PUBLIC_S3_CODES))


def _read_receipt(log_text):
    """Return a kind (passed, failed, unreadable or invalid) and the receipt when it is well formed."""
    if not isinstance(log_text, str) or not log_text.strip():
        return "unreadable", None
    lines = log_text.splitlines()
    if len(lines) != 1:
        return "invalid", None
    try:
        receipt = json.loads(lines[0])
    except (ValueError, RecursionError):
        return "invalid", None
    if not isinstance(receipt, dict):
        return "invalid", None
    if receipt.get("passed") is True and _is_success_receipt(receipt):
        return "passed", receipt
    if receipt.get("passed") is False and _is_failure_receipt(receipt):
        return "failed", receipt
    return "invalid", None


def _job_is_as_rendered(job, run_id, plan_id, epoch, image):
    """Compare identity, image, command and environment with a fresh render.

    Volumes and hostAliases are not compared, so this catches a mistaken or test Job, not a hostile one.
    """
    containers = _dig(job, "spec", "template", "spec", "containers")
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
        return False
    actual = containers[0]
    env = actual.get("env")
    if not isinstance(env, list) or not all(isinstance(item, dict) for item in env):
        return False
    plain = {item.get("name"): item.get("value") for item in env if isinstance(item.get("name"), str)}
    annotations = _dig(job, "spec", "template", "metadata", "annotations")
    try:
        expected = attachment_job.job(
            image, run_id, plan_id, epoch, plain.get("PGHOSTADDR"),
            _dig(annotations, attachment_job.EGRESS_UID_ANNOTATION),
            with_approved_extras="ATTACHMENT_APPROVED_EXTRA_KEYS_FILE" in plain)
    except ValueError:
        return False
    wanted = expected["spec"]["template"]["spec"]["containers"][0]
    return (_matching(KUBERNETES_UID, _dig(job, "metadata", "uid")) is not None
            and _dig(job, "metadata", "name") == expected["metadata"]["name"]
            and _dig(job, "metadata", "namespace") == NAMESPACE
            and _dig(job, "metadata", "labels", attachment_job.RUN_LABEL) == run_id
            and _dig(annotations, attachment_job.PLAN_ANNOTATION) == plan_id
            and _dig(annotations, attachment_job.EPOCH_ANNOTATION) == str(epoch)
            and all(_dig(job, "spec", field) == expected["spec"][field]
                    for field in ("backoffLimit", "parallelism", "completions"))
            and all(actual.get(field) == wanted[field] for field in ("name", "image", "command", "env")))


def _pod_is_as_rendered(pod, job_uid, image):
    owners = _dig(pod, "metadata", "ownerReferences")
    containers = _dig(pod, "spec", "containers")
    return (_matching(KUBERNETES_UID, _dig(pod, "metadata", "uid")) is not None
            and isinstance(owners, list) and any(_dig(owner, "uid") == job_uid for owner in owners)
            and isinstance(containers, list) and len(containers) == 1 and _dig(containers[0], "image") == image)


def judge(run_id, plan_id, epoch, image, job, pods, log_text):
    """Decide from what was read, which may be None or malformed and then leads to UNKNOWN.

    The caller validates run_id, plan_id, epoch and image first; the command line does.
    """
    result = {"schema": SCHEMA, "run_id": run_id, "plan_id": plan_id, "epoch": epoch,
              "verdict": UNKNOWN, "reason_code": None,
              "image_digest": image.rsplit("@", 1)[1], "job_uid": None, "pod_uid": None,
              "started_at": None, "finished_at": None, "job_failure_reason": None,
              "container_exit_code": None, "receipt": None}

    def unknown(reason):
        result["reason_code"] = reason
        return result

    if not isinstance(job, dict):
        return unknown("job_unreadable")
    if not _job_is_as_rendered(job, run_id, plan_id, epoch, image):
        return unknown("job_not_as_rendered")
    job_uid = result["job_uid"] = job["metadata"]["uid"]
    result["started_at"] = _matching(TIMESTAMP, _dig(job, "status", "startTime"))

    conditions = _dig(job, "status", "conditions")
    conditions = conditions if isinstance(conditions, list) else []
    terminal = {kind: next((c for c in conditions if _dig(c, "type") == kind and _dig(c, "status") == "True"), None)
                for kind in ("Complete", "Failed")}
    complete, failed = terminal["Complete"], terminal["Failed"]
    if complete and failed:
        return unknown("job_conditions_conflict")
    if not complete and not failed:
        return unknown("job_not_finished")
    result["finished_at"] = _matching(TIMESTAMP, _dig(complete or failed, "lastTransitionTime"))
    if failed and failed.get("reason") is not None:
        result["job_failure_reason"] = failed["reason"] if failed["reason"] in JOB_FAILURE_REASONS else "unclassified"

    if not isinstance(pods, list) or len(pods) != 1:
        return unknown("pod_count_not_one")
    pod = pods[0]
    if not _pod_is_as_rendered(pod, job_uid, image):
        return unknown("pod_not_as_rendered")
    result["pod_uid"] = pod["metadata"]["uid"]
    statuses = _dig(pod, "status", "containerStatuses")
    status = statuses[0] if isinstance(statuses, list) and len(statuses) == 1 else None
    exit_code = _dig(status, "state", "terminated", "exitCode")
    if type(exit_code) is not int:
        return unknown("pod_not_terminated")
    result["container_exit_code"] = exit_code
    image_id = _dig(status, "imageID")
    if not isinstance(image_id, str) or not image_id.endswith("@" + result["image_digest"]):
        return unknown("image_digest_mismatch")

    kind, receipt = _read_receipt(log_text)
    if kind in ("unreadable", "invalid"):
        return unknown("receipt_" + kind)
    succeeded_count, failed_count = _dig(job, "status", "succeeded"), _dig(job, "status", "failed")
    if complete and kind == "passed" and exit_code == 0 and succeeded_count == 1 and not failed_count:
        verdict, reason = SUCCEEDED, "receipt_passed"
    elif failed and kind == "failed" and exit_code == 1 and failed_count == 1 and not succeeded_count:
        verdict, reason = FAILED, "receipt_failed"
    else:
        return unknown("job_and_receipt_disagree")
    result.update(verdict=verdict, reason_code=reason, receipt=receipt)
    return result


def _kubectl(kubeconfig, *arguments):
    """Return stdout, or None when the read failed. The error text is dropped, not repeated."""
    command = ["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), "-n", NAMESPACE, *arguments]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=KUBECTL_TIMEOUT_SECONDS, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _read_json(kubeconfig, *arguments):
    output = _kubectl(kubeconfig, *arguments)
    try:
        return json.loads(output) if output else None
    except ValueError:
        return None


def read_and_judge(run_id, plan_id, epoch, image, kubeconfig=None):
    name = attachment_job.job_name(run_id)
    job = _read_json(kubeconfig, "get", "job", name, "-o", "json")
    listing = _read_json(kubeconfig, "get", "pods", "-l", f"batch.kubernetes.io/job-name={name}", "-o", "json")
    pods = _dig(listing, "items")
    log_text = None
    if isinstance(pods, list) and len(pods) == 1:
        pod_name = _matching(POD_NAME, _dig(pods[0], "metadata", "name"))
        if pod_name:
            log_text = _kubectl(kubeconfig, "logs", f"pod/{pod_name}", "-c", attachment_job.CONTAINER,
                                f"--limit-bytes={LOG_LIMIT_BYTES}")
    return judge(run_id, plan_id, epoch, image, job, pods, log_text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--image", required=True)
    parser.add_argument("--kubeconfig")
    args = parser.parse_args()
    try:
        attachment_job.validate_run_id(args.run_id)
        attachment_job.validate_plan(args.plan_id, args.epoch)
        attachment_job.validate_image(args.image)
    except ValueError as error:
        parser.error(str(error))
    result = read_and_judge(args.run_id, args.plan_id, args.epoch, args.image, args.kubeconfig)
    print(json.dumps(result))
    return EXIT_CODES[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
