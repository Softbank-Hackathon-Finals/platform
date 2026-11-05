"""Remove each safety property from a temporary copy and require the Job and verdict tests to fail.

Proves those tests measure the properties instead of merely passing. The repository files are
never edited, and only the standard library is needed. The image script requires Python 3.12.

    uv run --no-project --python 3.12 python scripts/tests/attachment_job_mutation_check.py OUTPUT.json
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKLOADS = HERE.parents[1] / "infra/envs/target-workloads"
SCRIPTS = HERE.parent
JOB, VERDICT, DOCKERFILE = "attachment_job.py", "attachment_verdict.py", "attachment-copy.Dockerfile"
IMAGE = "attachment_image.py"
# Only a test method counts. A crash at import or in setup measures nothing.
RESULT_LINE = re.compile(r"^(?:FAIL|ERROR): (test_\w+) \(test_attachment_(?:job|verdict|image)\.", re.MULTILINE)

MUTATIONS = [
    ("one_attempt", JOB, '"backoffLimit": 0', '"backoffLimit": 1'),
    ("one_pod_at_a_time", JOB, '"parallelism": 1', '"parallelism": 2'),
    ("one_completion", JOB, '"completions": 1', '"completions": 2'),
    ("deadline_present", JOB, '"activeDeadlineSeconds": DEADLINE_SECONDS, ', ""),
    ("cleanup_time_present", JOB, '"ttlSecondsAfterFinished": CLEANUP_SECONDS', '"suspend": False'),
    ("no_restart", JOB, '"restartPolicy": "Never"', '"restartPolicy": "OnFailure"'),
    ("no_service_account_token", JOB, '"automountServiceAccountToken": False', '"automountServiceAccountToken": True'),
    ("non_root", JOB, '"runAsNonRoot": True', '"runAsNonRoot": False'),
    ("seccomp_profile", JOB, '"seccompProfile": {"type": "RuntimeDefault"}', '"fsGroup": 10001'),
    ("no_privilege_escalation", JOB, '"allowPrivilegeEscalation": False', '"allowPrivilegeEscalation": True'),
    ("read_only_root", JOB, '"readOnlyRootFilesystem": True', '"readOnlyRootFilesystem": False'),
    ("all_capabilities_dropped", JOB, '"capabilities": {"drop": ["ALL"]}', '"capabilities": {"drop": []}'),
    ("image_validated", JOB, "    validate_image(image)\n    validate_run_id(run_id)\n", "    validate_run_id(run_id)\n"),
    ("full_digest_required", JOB, "@sha256:[0-9a-f]{64}", "@sha256:[0-9a-f]{1,64}"),
    ("digest_required", JOB, r'[a-z0-9._/:-]*@sha256:[0-9a-f]{64}"', r'[a-z0-9._/:-]*(@sha256:[0-9a-f]{64})?"'),
    ("run_id_validated", JOB, "    validate_image(image)\n    validate_run_id(run_id)\n", "    validate_image(image)\n"),
    ("run_id_length", JOB, "len(run_id) > MAX_RUN_ID_LENGTH", "len(run_id) > 253"),
    ("plan_validated", JOB, "    validate_plan(plan_id, epoch)\n    if not isinstance(egress_service_uid",
     "    if not isinstance(egress_service_uid"),
    ("plan_pattern_exact", JOB, r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}"', r'.*"'),
    ("epoch_positive", JOB, "type(epoch) is not int or epoch < 1", "type(epoch) is not int"),
    ("epoch_is_integer", JOB, "type(epoch) is not int or epoch < 1", "epoch < 1"),
    ("plan_recorded", JOB, "PLAN_ANNOTATION: plan_id,", ""),
    ("epoch_recorded", JOB, "EPOCH_ANNOTATION: str(epoch)", ""),
    ("waits_for_the_target_route", JOB, '"initContainers": [{', '"initContainers": [][:0] and [{'),
    ("wait_targets_the_object_store", JOB, "({TARGET_HOST!r}, {TARGET_PORT})", "('other', 1)"),
    ("wait_is_bounded", JOB, "for _ in range(30):", "for _ in range(9999):"),
    ("wait_runs_restricted", JOB, '"securityContext": container_security,\n                    "resources": {"requests": {"cpu": "10m"',
     '"resources": {"requests": {"cpu": "10m"'),
    ("service_uid_exact", JOB, "str(uuid.UUID(egress_service_uid)) != egress_service_uid",
     "not egress_service_uid"),
    ("secret_not_inlined", JOB, '_secret_env("AWS_SECRET_ACCESS_KEY", SOURCE_SECRET, "aws_secret_access_key")',
     '{"name": "AWS_SECRET_ACCESS_KEY", "value": "inline"}'),
    ("private_bucket_not_inlined", JOB, '_secret_env("ATTACHMENT_SOURCE_BUCKET", SOURCE_SECRET, "bucket")',
     '{"name": "ATTACHMENT_SOURCE_BUCKET", "value": "inline"}'),
    ("session_token_passed", JOB, '        _secret_env("AWS_SESSION_TOKEN", SOURCE_SECRET, "aws_session_token"),\n', ""),
    ("session_token_mandatory", JOB, '{"secretKeyRef": {"name": secret, "key": key}}',
     '{"secretKeyRef": {"name": secret, "key": key, "optional": True}}'),
    ("target_secret_name", JOB, 'TARGET_SECRET = "target-object-client"', 'TARGET_SECRET = "other"'),
    ("object_client_label", JOB, ', "bravo-object-client": "true"', ""),
    ("stderr_dropped", JOB, "attachment_copy.py 2>/dev/null", "attachment_copy.py"),
    ("verified_tls", JOB, '{"name": "PGSSLMODE", "value": database["sslmode"]}', '{"name": "PGSSLMODE", "value": "require"}'),
    ("egress_address_used", JOB, '        {"name": "PGHOSTADDR", "value": database["hostaddr"]},\n', ""),
    ("certificate_host_used", JOB, '{"name": "PGHOST", "value": database["host"]}', '{"name": "PGHOST", "value": database["hostaddr"]}'),
    ("replication_role_used", JOB, '{"name": "PGUSER", "value": database["user"]}', '{"name": "PGUSER", "value": "chat_app"}'),
    ("ca_path_matches_mount", JOB, 'f"{ca_directory}/{ca[\'key\']}"', '"/tls/other/ca.crt"'),
    ("ca_mount_read_only", JOB, '"mountPath": ca_directory, "readOnly": True', '"mountPath": ca_directory'),
    ("extras_off_by_default", JOB, "if with_approved_extras:", "if True:"),
    ("extras_mount_read_only", JOB, '"mountPath": extras_directory, "readOnly": True', '"mountPath": extras_directory'),
    ("session_outlives_deadline", JOB, "SOURCE_SESSION_SECONDS = 1800", "SOURCE_SESSION_SECONDS = 300"),
    ("target_address", JOB, 'TARGET_HOST = f"seaweedfs.', 'TARGET_HOST = f"other.'),
    ("memory_above_peak", JOB, '"memory": "256Mi"', '"memory": "64Mi"'),
    ("policy_read_only", JOB, '"Action": "s3:GetObject", "Resource": objects', '"Action": "s3:*", "Resource": objects'),
    ("policy_exact_objects", JOB, 'objects = bucket_arn + "/*"', 'objects = "*"'),
    ("policy_decrypt_through_s3", JOB, '"Condition": {"StringEquals": {"kms:ViaService": f"s3.{key.group(1)}.amazonaws.com"}}',
     '"Condition": {}'),
    ("policy_denies_other_actions", JOB, '        {"Effect": "Deny", "NotAction": SOURCE_SESSION_ACTIONS, "Resource": "*"},\n', ""),
    ("policy_denies_other_buckets", JOB,
     '        {"Effect": "Deny", "Action": ["s3:ListBucket", "s3:GetObject"], "NotResource": [bucket_arn, objects]},\n', ""),
    ("policy_bucket_arn_exact", JOB, r'r"arn:aws:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"', r'r".*"'),
    ("policy_key_arn_exact", JOB, r':key/[0-9a-f-]{36}"', r':[a-z]+/[0-9a-f-]{36}"'),

    ("image_pinned_base", DOCKERFILE, "@sha256:da54eb86849fe19be7584d8b3f291fcf0c53c196760ab3d70dc6382f681ef9c0", ""),
    ("image_runs_non_root", DOCKERFILE, "USER 10001:10001\n", ""),
    ("image_hash_locked", DOCKERFILE, " --require-hashes", ""),
    ("image_app_searchable", DOCKERFILE, "WORKDIR /app\n", ""),
    ("image_has_copy_modules", DOCKERFILE, " attachment_stores.py", ""),

    ("image_source_must_be_reviewed", IMAGE,
     '    run(["git", "-C", str(repo), "merge-base", "--is-ancestor", source, "origin/develop"], "source_not_reviewed")\n', ""),
    ("image_source_name_checked", IMAGE, "    executor_image.source_sha(source)\n    run([", "    run(["),
    ("image_context_inventory_exact", IMAGE, "if sorted(m.name for m in stored.getmembers() if m.isfile()) != sorted(CONTEXT_FILES):", "if False:"),
    ("image_base_pinned", IMAGE, r'@sha256:[0-9a-f]{64}", bases[0])', r'.*", bases[0])'),
    ("image_single_base", IMAGE, "len(bases) != 1 or ", "not bases or "),
    ("image_amd64_only", IMAGE, '"--platform", "linux/amd64", "--provenance=mode=max"', '"--provenance=mode=max"'),
    ("image_provenance", IMAGE, '"--provenance=mode=max",', '"--provenance=mode=min",'),
    ("image_sbom", IMAGE, 'f"--sbom=generator={SBOM_GENERATOR}",', '"--sbom=false",'),
    ("image_named_for_attestation", IMAGE, '"-t", f"{IMAGE_NAME}:sha-{source}",', ""),
    ("image_revision_label", IMAGE, '"--label", f"org.opencontainers.image.revision={source}",', ""),
    ("image_oci_output", IMAGE, "tar=false", "tar=true"),
    ("image_mirror_keeps_digest", IMAGE, "docker-image://mirror.gcr.io/library/{base}", "docker-image://mirror.gcr.io/library/python:latest"),
    ("scan_vulnerabilities_and_secrets", IMAGE, '"--scanners", "vuln,secret"', '"--scanners", "vuln"'),
    ("scan_severity", IMAGE, '"--severity", "HIGH,CRITICAL"', '"--severity", "CRITICAL"'),
    ("scan_stops_on_findings", IMAGE, '"--exit-code", "1"', '"--exit-code", "0"'),
    ("scan_read_only", IMAGE, "{layout}:/layout:ro", "{layout}:/layout"),
    ("scan_image_pinned", IMAGE, ":0.70.0@sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e", ":0.70.0"),
    ("scan_ran", IMAGE, '        run(scan_command(layout, scan), "image_scan")\n', ""),
    ("scan_report_checked", IMAGE, 'summary = executor_image.scan_summary(json.loads((scan / "trivy.json").read_text()))', "summary = {}"),
    ("layout_verified", IMAGE, "inventory = executor_image.verify_layout(layout, digest, source)", "inventory = None"),
    ("archive_verified", IMAGE, "if verify_archive(staged, digest, source, work) != inventory:", "if False:"),
    ("archive_names_checked", IMAGE, "not all(ARCHIVE_MEMBER.fullmatch(name) for name in names)", "False"),
    ("archive_links_refused", IMAGE, "not all(member.isfile() for member in stored.getmembers())", "False"),
    ("archive_duplicates_refused", IMAGE, "len(names) != len(set(names))", "False"),
    ("archive_extraction_filtered", IMAGE, 'stored.extractall(temporary, filter="data")\n        return executor_image.verify_layout(Path(temporary), digest, source)',
     'stored.extractall(temporary, filter="data")\n        return {}'),
    ("archive_owner_cleared", IMAGE, "member.mode = 0o644", "member.mode = 0o600"),
    ("probe_ran", IMAGE, "        runtime_probe(staged, digest, source)\n", ""),
    ("probe_result_checked", IMAGE, 'if b"attachment_runtime_probe=pass" not in output:', "if False:"),
    ("probe_has_no_network", IMAGE, '"--network", "none", ', ""),
    ("probe_read_only", IMAGE, '"--read-only",', ""),
    ("probe_no_capabilities", IMAGE, '"--cap-drop", "ALL", ', ""),
    ("probe_image_removed", IMAGE, "        remove_loaded_image(tag)\n", "        pass\n"),
    ("output_not_overwritten", IMAGE, "if archive.exists() or receipt_path.exists():", "if False:"),
    ("failure_drops_tool_output", IMAGE, 'raise Failure(phase + "_failed")', 'raise Failure(result.stdout.decode())'),
    ("unexpected_error_text_dropped", IMAGE, '"error_code": "invalid_local_state"', '"error_code": "invalid_local_state", "detail": str(sys.exc_info()[1])'),
    ("python_version_checked", IMAGE, "if PYTHON < (3, 12):", "if False:"),
    ("private_umask", IMAGE, "        os.umask(0o077)\n", ""),
    ("receipt_binds_archive", IMAGE, '"archive_sha256": checksum', '"archive_sha256": "0"'),
    ("receipt_makes_no_vm_claim", IMAGE, '"target_import_verified": False', '"target_import_verified": True'),
    ("import_keeps_attestations", IMAGE, '"--all-platforms", ', ""),

    ("job_identity_checked", VERDICT, "if not _job_is_as_rendered(job, run_id, plan_id, epoch, image):", "if False:"),
    ("job_uid_required", VERDICT, '_matching(KUBERNETES_UID, _dig(job, "metadata", "uid")) is not None', "True"),
    ("job_name_compared", VERDICT, '_dig(job, "metadata", "name") == expected["metadata"]["name"]', "True"),
    ("job_namespace_compared", VERDICT, '_dig(job, "metadata", "namespace") == NAMESPACE', "True"),
    ("job_run_label_compared", VERDICT, '_dig(job, "metadata", "labels", attachment_job.RUN_LABEL) == run_id', "True"),
    ("job_plan_compared", VERDICT, '_dig(annotations, attachment_job.PLAN_ANNOTATION) == plan_id', "True"),
    ("job_epoch_compared", VERDICT, '_dig(annotations, attachment_job.EPOCH_ANNOTATION) == str(epoch)', "True"),
    ("result_names_the_plan", VERDICT, '"plan_id": plan_id, "epoch": epoch,', '"plan_id": None, "epoch": None,'),
    ("job_attempts_compared", VERDICT, '("backoffLimit", "parallelism", "completions")', '("parallelism", "completions")'),
    ("job_parallelism_compared", VERDICT, '("backoffLimit", "parallelism", "completions")', '("backoffLimit", "completions")'),
    ("job_completions_compared", VERDICT, '("backoffLimit", "parallelism", "completions")', '("backoffLimit", "parallelism")'),
    ("job_image_compared", VERDICT, '("name", "image", "command", "env")', '("name", "command", "env")'),
    ("job_command_compared", VERDICT, '("name", "image", "command", "env")', '("name", "image", "env")'),
    ("job_env_compared", VERDICT, '("name", "image", "command", "env")', '("name", "image", "command")'),
    ("one_container", VERDICT, "len(containers) != 1 or not isinstance(containers[0], dict)",
     "not containers or not isinstance(containers[0], dict)"),
    ("condition_must_be_true", VERDICT, ' and _dig(c, "status") == "True"', ""),
    ("conflict_detected", VERDICT, '    if complete and failed:\n        return unknown("job_conditions_conflict")\n', ""),
    ("unfinished_detected", VERDICT, '    if not complete and not failed:\n        return unknown("job_not_finished")\n', ""),
    ("job_failure_reason_closed", VERDICT, 'failed["reason"] if failed["reason"] in JOB_FAILURE_REASONS else "unclassified"',
     'failed["reason"]'),
    ("timestamps_not_repeated", VERDICT, '_matching(TIMESTAMP, _dig(job, "status", "startTime"))',
     '_dig(job, "status", "startTime")'),
    ("one_pod", VERDICT, "len(pods) != 1:\n        return unknown", "len(pods) < 1:\n        return unknown"),
    ("pod_identity_checked", VERDICT, "if not _pod_is_as_rendered(pod, job_uid, image):", "if False:"),
    ("pod_owner_compared", VERDICT, 'any(_dig(owner, "uid") == job_uid for owner in owners)', "True"),
    ("pod_image_compared", VERDICT, '_dig(containers[0], "image") == image', "True"),
    ("pod_terminated", VERDICT, '    if type(exit_code) is not int:\n        return unknown("pod_not_terminated")\n', ""),
    ("runtime_digest_compared", VERDICT, 'not image_id.endswith("@" + result["image_digest"])', "False"),
    ("one_log_line", VERDICT, "if len(lines) != 1:", "if not lines:"),
    ("empty_log_unreadable", VERDICT, "or not log_text.strip()", ""),
    ("receipt_is_object", VERDICT, '    if not isinstance(receipt, dict):\n        return "invalid", None\n', ""),
    ("success_fields_exact", VERDICT, 'set(receipt) != SUCCESS_FIELDS or ', ""),
    ("counts_are_real_integers", VERDICT, 'type(receipt[field]) is int and ', 'isinstance(receipt[field], (int, float)) and '),
    ("counts_not_negative", VERDICT, ' and receipt[field] >= 0', ""),
    ("at_least_one_object", VERDICT, "1 <= total <= MAX_OBJECTS", "total <= MAX_OBJECTS"),
    ("object_limit", VERDICT, "1 <= total <= MAX_OBJECTS", "1 <= total"),
    ("destination_equals_source", VERDICT, ' and receipt["destination_count"] == total', ""),
    ("referenced_plus_extras", VERDICT,
     '            and receipt["database_referenced_count"] + receipt["operator_approved_extra_count"] == total\n', ""),
    ("copied_plus_reused", VERDICT, '            and receipt["copied"] + receipt["reused"] == total\n', ""),
    ("byte_limit", VERDICT, '            and receipt["bytes_verified"] <= MAX_BYTES\n', ""),
    ("inventory_hash_format", VERDICT, '            and _is_sha256(receipt["key_content_inventory_sha256"])\n', ""),
    ("hash_count", VERDICT, " and len(hashes) == total", ""),
    ("hash_format", VERDICT, "all(_is_sha256(value) for value in hashes) and ", ""),
    ("hash_order", VERDICT, " and hashes == sorted(hashes)", ""),
    ("failure_fields_exact", VERDICT, 'set(receipt) != FAILURE_FIELDS or ', ""),
    ("output_suppression_required", VERDICT, ' or receipt["raw_output_suppressed"] is not True', ""),
    ("failure_stage_closed", VERDICT, 'receipt["stage"] in FAILURE_STAGES\n            and ', ""),
    ("failure_reason_closed", VERDICT, 'and (reason in FAILURE_REASONS or MISSING_SETTING.fullmatch(reason) is not None)', ""),
    ("failure_category_closed", VERDICT, 'and (category is None or _matching(EXCEPTION_NAME, category) is not None)', ""),
    ("failure_code_closed", VERDICT, 'and (code is None or code == "unclassified" or code in PUBLIC_S3_CODES)', ""),
    ("success_needs_complete_job", VERDICT, 'if complete and kind == "passed"', 'if kind == "passed"'),
    ("success_needs_exit_zero", VERDICT, 'kind == "passed" and exit_code == 0 and', 'kind == "passed" and'),
    ("success_needs_one_success", VERDICT, 'succeeded_count == 1 and not failed_count', "not failed_count"),
    ("success_needs_no_failure", VERDICT, 'succeeded_count == 1 and not failed_count', "succeeded_count == 1"),
    ("failure_needs_failed_job", VERDICT, 'elif failed and kind == "failed"', 'elif kind == "failed"'),
    ("failure_needs_exit_one", VERDICT, 'kind == "failed" and exit_code == 1 and', 'kind == "failed" and'),
    ("failure_needs_one_failure", VERDICT, "failed_count == 1 and not succeeded_count", "not succeeded_count"),
    ("failure_needs_no_success", VERDICT, "failed_count == 1 and not succeeded_count", "failed_count == 1"),
    ("receipt_only_after_decision", VERDICT, '        return unknown("job_and_receipt_disagree")\n',
     '        result["receipt"] = receipt\n        return unknown("job_and_receipt_disagree")\n'),
    ("unknown_exit_code", VERDICT, "UNKNOWN: 2", "UNKNOWN: 0"),
    ("failed_exit_code", VERDICT, "FAILED: 1", "FAILED: 0"),
    ("kubectl_failure_is_not_output", VERDICT, "completed.stdout if completed.returncode == 0 else None", "completed.stdout"),
    ("kubectl_timeout_handled", VERDICT, "except (OSError, ValueError, subprocess.SubprocessError):", "except (OSError, ValueError):"),
    ("kubectl_missing_handled", VERDICT, "except (OSError, ValueError, subprocess.SubprocessError):",
     "except (ValueError, subprocess.SubprocessError):"),
    ("kubectl_has_timeout", VERDICT, "timeout=KUBECTL_TIMEOUT_SECONDS", "timeout=None"),
    ("bad_json_is_not_output", VERDICT, "    except ValueError:\n        return None\n", "    except KeyError:\n        return None\n"),
    ("log_is_bounded", VERDICT, '"-c", attachment_job.CONTAINER,\n                                f"--limit-bytes={LOG_LIMIT_BYTES}")',
     '"-c", attachment_job.CONTAINER)'),
    ("pod_name_validated", VERDICT, '_matching(POD_NAME, _dig(pods[0], "metadata", "name"))', '_dig(pods[0], "metadata", "name")'),
    ("inputs_validated_before_reading", VERDICT, "        attachment_job.validate_run_id(args.run_id)\n", ""),
    ("plan_validated_before_reading", VERDICT, "        attachment_job.validate_plan(args.plan_id, args.epoch)\n", ""),
    ("image_validated_before_reading", VERDICT, "        attachment_job.validate_image(args.image)\n", ""),
]


def run_tests(directory):
    environment = {**os.environ, "T8_ATTACH_MODULE_DIR": str(directory), "T8_ATTACH_SCRIPTS_DIR": str(directory),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(HERE),
                           "-p", "test_attachment_[jvi]*.py"], capture_output=True, text=True, timeout=300,
                          env=environment, check=False)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: attachment_job_mutation_check.py OUTPUT.json")
    with tempfile.TemporaryDirectory(prefix="t8-job-mutation-") as temporary:
        directory = Path(temporary, "workloads")
        shutil.copytree(WORKLOADS, directory, ignore=shutil.ignore_patterns("testing", "__pycache__"))
        for name in (IMAGE, "executor_image.py"):
            shutil.copy(SCRIPTS / name, directory / name)
        baseline = run_tests(directory)
        if baseline.returncode:
            print(baseline.stderr[-2000:], file=sys.stderr)
            raise SystemExit("baseline tests failed")
        count = re.search(r"Ran (\d+) tests", baseline.stderr).group(1)
        results = []
        for name, file_name, before, after in MUTATIONS:
            path = directory / file_name
            original = path.read_text()
            if original.count(before) < 1:
                raise SystemExit(f"mutation {name} no longer applies to {file_name}")
            path.write_text(original.replace(before, after, 1))
            outcome = run_tests(directory)
            path.write_text(original)
            failing = sorted(set(RESULT_LINE.findall(outcome.stderr)))
            results.append({"removed_property": name, "module": file_name, "suite_exit_code": outcome.returncode,
                            "failing_test_count": len(failing), "failing_tests": failing})
            print(("caught   " if failing else "SURVIVED ") + name, flush=True)
    survivors = [item["removed_property"] for item in results if not item["failing_tests"]]
    Path(sys.argv[1]).write_text(json.dumps({
        "baseline_passed": True, "baseline_tests": int(count), "mutation_count": len(results),
        "survivors": survivors, "mutations": results}, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(results) - len(survivors)}/{len(results)} caught")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
