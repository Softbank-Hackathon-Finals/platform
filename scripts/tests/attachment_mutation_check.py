"""Remove each safety check from a temporary copy of the modules and require the tests to fail.

Proves the attachment copy tests measure the checks instead of merely passing. The
repository modules are never edited. Containers are shared across runs and removed
at the end. Run with the same SDKs as the E2E test:

    uv run --no-project --python 3.12 --with boto3==1.43.123 --with 'psycopg[binary]==3.3.6' \
        python scripts/tests/attachment_mutation_check.py OUTPUT.json
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import attachment_containers

SYNC, STORES, COPY = "sync_attachments.py", "attachment_stores.py", "attachment_copy.py"
HERE = Path(__file__).resolve().parent
# Only a test method counts. A crash in setUpModule or an import error measures nothing.
RESULT_LINE = re.compile(r"^(?:FAIL|ERROR): (test_\w+) \(test_attachment_copy\w*\.")

MUTATIONS = [
    ("source_inventory_compare", SYNC,
     ("    if set(source.list_keys()) != expected:\n"
     "        raise ValueError('source bucket and database attachment inventory differ')\n"), ""),
    ("unrelated_destination_check", SYNC,
     ("    if not present <= expected:\n"
     "        raise ValueError('destination contains unrelated objects; preserve and reconcile')\n"), ""),
    ("existing_bytes_compare", SYNC,
     ("            if destination.read(key) != content:\n"
     "                raise ValueError('destination conflict; existing object is preserved')\n"), ""),
    ("destination_readback", SYNC,
     ("        if destination.read(key) != content:\n"
     "            raise ValueError('destination byte verification failed')\n"), ""),
    ("source_reread", SYNC,
     ("        if source.read(key) != content:\n"
     "            raise ValueError('source changed during transfer; require quiescent retry')\n"), ""),
    ("final_inventory_compare", SYNC,
     ("    if set(source.list_keys()) != expected or set(destination.list_keys()) != expected:\n"
     "        raise ValueError('final object inventory changed')\n"), ""),
    ("object_count_limit", SYNC, " or len(expected) > MAX_OBJECTS", ""),
    ("empty_inventory_check", SYNC, "if not expected or len(expected)", "if len(expected)"),
    ("single_object_limit", SYNC, " or len(content) > MAX_BYTES", ""),
    ("running_total_limit", SYNC,
     "        if total > MAX_BYTES:\n            raise ValueError('bounded transfer size exceeded')\n", ""),
    ("reuse_of_existing_objects", SYNC, "if key in present:", "if False:"),
    ("conditional_create", STORES, ', IfNoneMatch="*"', ""),
    ("bounded_read", STORES, "body.read(MAX_BYTES + 1)", "body.read()"),
    ("listing_cap", STORES, "while len(found) <= MAX_OBJECTS:", "while True:"),
    ("cut_stream_check", STORES,
     ('        if len(content) < min(response["ContentLength"], MAX_BYTES + 1):\n'
     '            raise ValueError("incomplete object read")\n'), ""),
    ("expected_bucket_owner", STORES,
     'request = {"ExpectedBucketOwner": expected_owner} if expected_owner else {}', "request = {}"),
    ("source_has_no_write_method", STORES,
     '"""Read-only by construction: list and read are the whole surface."""',
     "def create(self, key, content):\n        self._client.put_object(Bucket=self._bucket, Key=key, Body=content)"),
    ("destination_uses_only_its_own_keys", STORES,
     ("aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,\n"
      "                          aws_session_token=None, "), ""),
    ("destination_put_not_retried", STORES, 'config=_client_config("path", max_retries=0)',
     'config=_client_config("path", max_retries=1)'),
    ("configured_endpoint_ignored", STORES, "                  ignore_configured_endpoint_urls=True,\n", ""),
    ("read_only_transaction", COPY, "    connection.read_only = True\n", ""),
    ("verified_tls_check", COPY,
     '        require_verified_tls(info.host, info.hostaddr, info.get_parameters().get("sslmode"))\n',
     "        pass\n"),
    ("verified_tls_checks_hostaddr", COPY, "not {host, hostaddr} <= LOOPBACK_HOSTS", "host not in LOOPBACK_HOSTS"),
    ("inventory_statement_timeout", COPY,
     '    connection.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")\n', ""),
    ("total_size_preflight", COPY,
     ("    if sum(source.sizes().values()) > MAX_BYTES:\n"
     '        raise ValueError("bounded transfer size exceeded")\n'), ""),
    ("unmeasured_field_removal", COPY, '    result.pop("source_write_calls", None)\n', ""),
    ("exact_extra_keys", COPY, "[line for line in", "[line.strip() for line in"),
    ("public_reason_allowlist", COPY,
     'reason = str(error) if isinstance(error, ValueError) and str(error) in PUBLIC_REASONS else "unclassified"',
     "reason = str(error)"),
    ("public_s3_code_allowlist", COPY,
     '    if code is not None and code not in PUBLIC_S3_CODES:\n        code = "unclassified"\n', ""),
    ("raw_exception_suppression", COPY, "raise describe_failure(stage, error) from None",
     "raise describe_failure(stage, error) from error"),
    ("same_bucket_guard", COPY,
     ("    if (source_endpoint, source_bucket) == (target_endpoint, target_bucket):\n"
     '        raise AttachmentCopyFailed("configuration", "source and destination are the same bucket")\n'), ""),
    ("bucket_owner_required", COPY,
     ('environ.get("ATTACHMENT_SOURCE_BUCKET_OWNER") if source_endpoint else required(\n'
     '        "ATTACHMENT_SOURCE_BUCKET_OWNER")'), 'environ.get("ATTACHMENT_SOURCE_BUCKET_OWNER")'),
    ("nonzero_exit_on_failure", COPY, "        return 1\n", "        return 0\n"),
]


def run_suite(module_dir, environment):
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(HERE), "-p", "test_attachment_copy*.py", "-v"],
        env={**os.environ, "T8_ATTACH_E2E": "1", "T8_ATTACH_ENV": json.dumps(environment),
             "T8_ATTACH_MODULE_DIR": str(module_dir), "PYTHONDONTWRITEBYTECODE": "1"},
        check=False, capture_output=True, text=True, timeout=600)
    failed = sorted({match.group(1) for match in map(RESULT_LINE.match, result.stderr.splitlines()) if match})
    return result.returncode, failed


def main(output):
    environment = attachment_containers.start()
    results, survivors = [], []
    try:
        with tempfile.TemporaryDirectory() as work:
            pristine = Path(work) / "pristine"
            pristine.mkdir()
            for name in (SYNC, STORES, COPY):
                shutil.copy(attachment_containers.WORKLOADS / name, pristine / name)
            code, failed = run_suite(pristine, environment)
            if code or failed:
                raise SystemExit(f"baseline must pass before mutating: {failed}")
            for name, module, old, new in MUTATIONS:
                mutated = Path(work) / name
                shutil.copytree(pristine, mutated)
                text = (mutated / module).read_text()
                if text.count(old) != 1:
                    raise SystemExit(f"mutation {name} no longer matches {module} exactly once")
                (mutated / module).write_text(text.replace(old, new))
                code, failed = run_suite(mutated, environment)
                results.append({"removed_check": name, "module": module, "suite_exit_code": code,
                                "failing_test_count": len(failed), "failing_tests": failed})
                if not failed:
                    survivors.append(name)
                print(name, "caught" if failed else "SURVIVED", len(failed), flush=True)
    finally:
        attachment_containers.stop(environment)
    Path(output).write_text(json.dumps({"baseline_passed": True, "mutation_count": len(results),
                                        "survivors": survivors, "mutations": results}, indent=2) + "\n")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
