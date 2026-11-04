"""Real objects: moto source, the deployed SeaweedFS image and the target schema in real Postgres.

Needs Docker and the SDKs, so default discovery skips it. Run it explicitly:

    T8_ATTACH_E2E=1 uv run --no-project --python 3.12 --with boto3==1.43.123 \
        --with 'psycopg[binary]==3.3.6' python -m unittest discover -s scripts/tests \
        -p 'test_attachment_copy*.py' -v
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import attachment_containers

WORKLOADS = Path(os.environ.get("T8_ATTACH_MODULE_DIR") or attachment_containers.WORKLOADS)
BUCKET = attachment_containers.BUCKET
# Every key and body in this file carries a marker, so a leak check needs no list of values.
KEY = "attachments/leak-marker-"
BODY = b"body-marker-"
ENV = {}
attachment_copy = attachment_stores = psycopg = None


def setUpModule():
    global attachment_copy, attachment_stores, psycopg
    if os.environ.get("T8_ATTACH_E2E") != "1":
        raise unittest.SkipTest("T8_ATTACH_E2E=1 starts Docker containers; see the module docstring")
    sys.path.append(str(WORKLOADS))
    import attachment_copy as copy_module
    import attachment_stores as store_module
    import psycopg as driver
    attachment_copy, attachment_stores, psycopg = copy_module, store_module, driver
    if "T8_ATTACH_ENV" in os.environ:
        ENV.update(json.loads(os.environ["T8_ATTACH_ENV"]))
        return
    ENV.update(attachment_containers.start())
    unittest.addModuleCleanup(attachment_containers.stop, ENV)


def client(role):
    endpoint, pair = {"source": (ENV["source_endpoint"], ENV["source_pair"]),
                      "target": (ENV["target_endpoint"], ENV["target_bootstrap"])}[role]
    return attachment_containers.s3_client(endpoint, *pair)


def admin_database():
    return psycopg.connect(**ENV["database"], autocommit=True)


def dsn():
    database = ENV["database"]
    return " ".join(f"{name}={database[name]}" for name in ("host", "port", "user", "password", "dbname"))


def clear(s3):
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for item in page.get("Contents", []):
            s3.delete_object(Bucket=BUCKET, Key=item["Key"])


def contents(s3):
    found = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for item in page.get("Contents", []):
            found[item["Key"]] = s3.get_object(Bucket=BUCKET, Key=item["Key"])["Body"].read()
    return found


class Case(unittest.TestCase):
    def setUp(self):
        self.source, self.target = client("source"), client("target")
        clear(self.source)
        clear(self.target)
        with admin_database() as connection:
            connection.execute("TRUNCATE attachments CASCADE")

    def put(self, s3, objects):
        for key, body in objects.items():
            s3.put_object(Bucket=BUCKET, Key=key, Body=body)

    def reference(self, keys):
        with admin_database() as connection:
            for key in keys:
                connection.execute(
                    "INSERT INTO attachments (id, object_key, filename, content_type, size)"
                    " VALUES (%s, %s, 'file', 'application/octet-stream', 0)", (uuid.uuid4(), key))

    def seed(self, objects):
        self.put(self.source, objects)
        self.reference(objects)

    def source_store(self):
        return attachment_stores.source_store(BUCKET, "us-east-1", endpoint_url=ENV["source_endpoint"])

    def target_store(self):
        return attachment_stores.destination_store(ENV["target_endpoint"], BUCKET, *ENV["target_client"])

    def source_state(self):
        return {item["Key"]: (item["ETag"], item["LastModified"])
                for item in self.source.list_objects_v2(Bucket=BUCKET).get("Contents", [])}

    def environment(self, **overrides):
        database = ENV["database"]
        values = {
            "PATH": os.environ["PATH"], "AWS_ACCESS_KEY_ID": ENV["source_pair"][0],
            "AWS_SECRET_ACCESS_KEY": ENV["source_pair"][1],
            "ATTACHMENT_SOURCE_ENDPOINT_URL": ENV["source_endpoint"], "ATTACHMENT_SOURCE_BUCKET": BUCKET,
            "ATTACHMENT_SOURCE_REGION": "us-east-1", "ATTACHMENT_TARGET_ENDPOINT_URL": ENV["target_endpoint"],
            "ATTACHMENT_TARGET_BUCKET": BUCKET, "ATTACHMENT_TARGET_ACCESS_KEY_ID": ENV["target_client"][0],
            "ATTACHMENT_TARGET_SECRET_ACCESS_KEY": ENV["target_client"][1], "PGHOST": database["host"],
            "PGPORT": database["port"], "PGUSER": database["user"], "PGPASSWORD": database["password"],
            "PGDATABASE": database["dbname"], "PGSSLMODE": "disable", "ATTACHMENT_DB_ALLOW_LOOPBACK_PLAINTEXT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        values.update(overrides)
        return values

    def assert_no_leak(self, text, environment):
        self.assertNotIn("leak-marker", text)
        self.assertNotIn(BODY.decode(), text)
        self.assertNotIn(BUCKET, text)
        for name in ("ATTACHMENT_SOURCE_ENDPOINT_URL", "ATTACHMENT_TARGET_ENDPOINT_URL", "AWS_ACCESS_KEY_ID",
                     "AWS_SECRET_ACCESS_KEY", "ATTACHMENT_TARGET_ACCESS_KEY_ID",
                     "ATTACHMENT_TARGET_SECRET_ACCESS_KEY", "PGPASSWORD"):
            self.assertNotIn(environment[name], text)
        for address in (environment["PGHOST"] + ":" + environment["PGPORT"], "127.0.0.1"):
            self.assertNotIn(address, text)

    def command(self, **overrides):
        environment = self.environment(**overrides)
        result = subprocess.run([sys.executable, str(WORKLOADS / "attachment_copy.py")], check=False,
                                env=environment, capture_output=True, text=True, timeout=300)
        self.assert_no_leak(result.stdout + result.stderr, environment)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        return result.returncode, json.loads(result.stdout)

    def assert_copied(self, **overrides):
        code, receipt = self.command(**overrides)
        self.assertEqual(code, 0, receipt)
        self.assertIs(receipt["passed"], True)
        return receipt

    def assert_rejected(self, reason, **overrides):
        code, receipt = self.command(**overrides)
        self.assertEqual(code, 1, receipt)
        self.assertIs(receipt["passed"], False)
        self.assertEqual(receipt["reason"], reason)
        self.assertIs(receipt["raw_output_suppressed"], True)
        return receipt


class CopyTests(Case):
    def test_copies_real_objects_and_leaves_the_source_untouched(self):
        objects = {KEY + "plain": BODY + b"plain", KEY + "with space/한글.png": BODY + bytes(range(256)),
                   KEY + "empty": b""}
        self.seed(objects)
        before = self.source_state()
        receipt = self.assert_copied()
        self.assertEqual(contents(self.target), objects)
        self.assertEqual(self.source_state(), before)
        self.assertEqual((receipt["copied"], receipt["reused"]), (3, 0))
        self.assertEqual((receipt["source_count"], receipt["destination_count"]), (3, 3))
        self.assertEqual(receipt["bytes_verified"], sum(len(body) for body in objects.values()))
        self.assertEqual(len(receipt["content_sha256"]), 3)
        self.assertNotIn("source_write_calls", receipt)

    def test_repeat_run_reuses_identical_objects(self):
        self.seed({KEY + "a": BODY + b"a", KEY + "b": BODY + b"b"})
        first = self.assert_copied()
        second = self.assert_copied()
        self.assertEqual((second["copied"], second["reused"]), (0, 2))
        self.assertEqual(first["key_content_inventory_sha256"], second["key_content_inventory_sha256"])

    def test_exact_limits_pass(self):
        count, total = attachment_copy.MAX_OBJECTS, attachment_copy.MAX_BYTES
        size = total // count
        objects = {f"{KEY}{index:03d}": bytes([index % 251]) * size for index in range(count)}
        objects[f"{KEY}{count - 1:03d}"] = b"\xff" * (total - size * (count - 1))
        self.seed(objects)
        receipt = self.assert_copied()
        self.assertEqual((receipt["copied"], receipt["bytes_verified"]), (count, total))
        self.assertEqual(contents(self.target), objects)

    def test_two_rows_sharing_one_object_count_once(self):
        self.put(self.source, {KEY + "shared": BODY})
        self.reference([KEY + "shared", KEY + "shared"])
        self.assertEqual(self.assert_copied()["database_referenced_count"], 1)

    def test_individually_approved_extra_from_a_file(self):
        self.seed({KEY + "referenced": BODY})
        self.put(self.source, {KEY + "canary": BODY + b"canary"})
        with tempfile.NamedTemporaryFile("w", suffix=".keys") as extras:
            extras.write(KEY + "canary\n")
            extras.flush()
            receipt = self.assert_copied(ATTACHMENT_APPROVED_EXTRA_KEYS_FILE=extras.name)
        self.assertEqual((receipt["database_referenced_count"], receipt["operator_approved_extra_count"]), (1, 1))

    def test_function_form_for_the_cutover_executor(self):
        self.seed({KEY + "a": BODY})
        environment = self.environment()
        for name in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE"):
            environment.pop(name)
        credentials = {"AWS_ACCESS_KEY_ID": environment["AWS_ACCESS_KEY_ID"],
                       "AWS_SECRET_ACCESS_KEY": environment["AWS_SECRET_ACCESS_KEY"]}
        with patch.dict(os.environ, credentials):
            receipt = attachment_copy.run_copy(environment, db_dsn=dsn())
            self.assertEqual(receipt["copied"], 1)
            clear(self.source)
            with self.assertRaises(attachment_copy.AttachmentCopyFailed) as caught:
                attachment_copy.run_copy(environment, db_dsn=dsn())
        self.assertEqual(caught.exception.reason, "source bucket and database attachment inventory differ")
        self.assertNotIn("leak-marker", str(caught.exception))


class InventoryRejectionTests(Case):
    def test_source_object_missing_from_database(self):
        self.seed({KEY + "a": BODY})
        self.put(self.source, {KEY + "unknown": BODY})
        self.assert_rejected("source bucket and database attachment inventory differ")
        self.assertEqual(contents(self.target), {})

    def test_database_reference_missing_from_source(self):
        self.seed({KEY + "a": BODY})
        self.reference([KEY + "lost"])
        self.assert_rejected("source bucket and database attachment inventory differ")
        self.assertEqual(contents(self.target), {})

    def test_empty_database_inventory(self):
        self.put(self.source, {KEY + "a": BODY})
        self.assert_rejected("bounded nonempty attachment inventory required")
        self.assertEqual(contents(self.target), {})

    def test_unrelated_destination_object_is_preserved(self):
        self.seed({KEY + "a": BODY})
        self.put(self.target, {KEY + "unrelated": BODY + b"keep"})
        self.assert_rejected("destination contains unrelated objects; preserve and reconcile")
        self.assertEqual(contents(self.target), {KEY + "unrelated": BODY + b"keep"})


class ConflictTests(Case):
    def test_existing_different_destination_bytes_are_preserved(self):
        self.seed({KEY + "a": BODY + b"new"})
        self.put(self.target, {KEY + "a": BODY + b"old"})
        self.assert_rejected("destination conflict; existing object is preserved")
        self.assertEqual(contents(self.target), {KEY + "a": BODY + b"old"})

    def test_create_refuses_an_existing_key_at_the_store(self):
        self.put(self.target, {KEY + "a": BODY + b"old"})
        with self.assertRaisesRegex(ValueError, "destination conflict"):
            self.target_store().create(KEY + "a", BODY + b"new")
        self.assertEqual(contents(self.target), {KEY + "a": BODY + b"old"})

    def test_create_that_races_the_listing_cannot_overwrite(self):
        self.seed({KEY + "a": BODY + b"new"})
        competitor = self.target

        class Racing:
            def __init__(self, inner):
                self.inner = inner

            def list_keys(self):
                return self.inner.list_keys()

            def read(self, key):
                return self.inner.read(key)

            def create(self, key, content):
                competitor.put_object(Bucket=BUCKET, Key=key, Body=BODY + b"winner")
                return self.inner.create(key, content)

        with self.assertRaisesRegex(ValueError, "destination conflict"):
            attachment_copy.copy_keys(self.source_store(), Racing(self.target_store()), [KEY + "a"])
        self.assertEqual(contents(self.target), {KEY + "a": BODY + b"winner"})

    def test_lost_put_response_is_not_reported_as_a_conflict(self):
        from botocore.exceptions import ConnectionClosedError
        self.seed({KEY + "a": BODY})
        store, applied = self.target_store(), []

        def lost_response(request, **_):
            # The server applied the PUT but the caller never saw the answer.
            if not applied:
                applied.append(True)
                self.target.put_object(Bucket=BUCKET, Key=KEY + "a", Body=BODY)
                raise ConnectionClosedError(endpoint_url=request.url)

        store._client.meta.events.register("before-send.s3.PutObject", lost_response)
        with self.assertRaises(ConnectionClosedError):
            attachment_copy.copy_keys(self.source_store(), store, [KEY + "a"])
        retry = attachment_copy.copy_keys(self.source_store(), self.target_store(), [KEY + "a"])
        self.assertEqual((retry["copied"], retry["reused"]), (0, 1))

    def test_corrupt_destination_write_cannot_report_success(self):
        self.seed({KEY + "a": BODY})
        store = self.target_store()

        class Corrupting:
            list_keys, read = store.list_keys, store.read

            def create(self, key, content):
                store.create(key, content + b"corrupt")

        with self.assertRaisesRegex(ValueError, "destination byte verification failed"):
            attachment_copy.copy_keys(self.source_store(), Corrupting(), [KEY + "a"])


class SourceChangeTests(Case):
    def mutating_source(self, change):
        inner = self.source_store()

        class Mutating:
            list_keys, sizes = inner.list_keys, inner.sizes
            reads = 0

            def read(self, key):
                content = inner.read(key)
                self.reads += 1
                if self.reads == 1:
                    change()
                return content

        return Mutating()

    def test_body_changed_during_transfer(self):
        self.seed({KEY + "a": BODY + b"first"})
        source = self.mutating_source(
            lambda: self.source.put_object(Bucket=BUCKET, Key=KEY + "a", Body=BODY + b"second"))
        with self.assertRaisesRegex(ValueError, "source changed during transfer"):
            attachment_copy.copy_keys(source, self.target_store(), [KEY + "a"])

    def test_object_added_during_transfer(self):
        self.seed({KEY + "a": BODY})
        source = self.mutating_source(
            lambda: self.source.put_object(Bucket=BUCKET, Key=KEY + "late", Body=BODY))
        with self.assertRaisesRegex(ValueError, "final object inventory changed"):
            attachment_copy.copy_keys(source, self.target_store(), [KEY + "a"])


class LimitTests(Case):
    def test_too_many_database_references_write_nothing(self):
        keys = [f"{KEY}{index}" for index in range(attachment_copy.MAX_OBJECTS + 1)]
        self.reference(keys)
        self.put(self.source, {keys[0]: BODY})
        self.assert_rejected("bounded nonempty attachment inventory required")
        self.assertEqual(contents(self.target), {})

    def test_too_many_source_objects_write_nothing(self):
        keys = [f"{KEY}{index}" for index in range(attachment_copy.MAX_OBJECTS + 2)]
        self.put(self.source, {key: BODY for key in keys})
        self.reference(keys[:attachment_copy.MAX_OBJECTS])
        self.assert_rejected("source bucket and database attachment inventory differ")
        self.assertEqual(contents(self.target), {})

    def test_listing_stops_one_past_the_object_limit(self):
        self.put(self.source, {f"{KEY}{index}": BODY for index in range(attachment_copy.MAX_OBJECTS + 5)})
        self.assertEqual(len(self.source_store().list_keys()), attachment_copy.MAX_OBJECTS + 1)

    def test_total_over_the_limit_writes_nothing(self):
        half = attachment_copy.MAX_BYTES // 2 + 1
        self.seed({KEY + "a": b"a" * half, KEY + "b": b"b" * half})
        self.assert_rejected("bounded transfer size exceeded")
        self.assertEqual(contents(self.target), {})

    def test_read_never_loads_more_than_one_past_the_limit(self):
        limit = attachment_copy.MAX_BYTES
        self.put(self.source, {KEY + "huge": b"h" * (limit + 1024)})
        self.assertEqual(len(self.source_store().read(KEY + "huge")), limit + 1)

    def test_single_object_over_the_limit_writes_nothing(self):
        self.seed({KEY + "huge": b"h" * (attachment_copy.MAX_BYTES + 1)})
        self.assert_rejected("bounded transfer size exceeded")
        self.assertEqual(contents(self.target), {})


    def understated_source(self):
        # The object grew after the listing: only synchronize() itself can still catch it.
        inner = self.source_store()

        class Understated:
            list_keys, read = inner.list_keys, inner.read

            def sizes(self):
                return {}

        return Understated()

    def test_object_over_the_limit_after_the_listing_writes_nothing(self):
        self.seed({KEY + "huge": b"h" * (attachment_copy.MAX_BYTES + 1)})
        with self.assertRaisesRegex(ValueError, "bounded binary source object required"):
            attachment_copy.copy_keys(self.understated_source(), self.target_store(), [KEY + "huge"])
        self.assertEqual(contents(self.target), {})

    def test_total_over_the_limit_after_the_listing_stops_copying(self):
        half = attachment_copy.MAX_BYTES // 2 + 1
        self.seed({KEY + "a": b"a" * half, KEY + "b": b"b" * half})
        with self.assertRaisesRegex(ValueError, "bounded transfer size exceeded"):
            attachment_copy.copy_keys(self.understated_source(), self.target_store(), [KEY + "a", KEY + "b"])
        self.assertEqual(list(contents(self.target)), [KEY + "a"])


class InterruptionTests(Case):
    def test_killed_process_then_rerun_reuses_finished_objects(self):
        objects = {f"{KEY}{index}": BODY + str(index).encode() for index in range(4)}
        self.seed(objects)
        # Abrupt process death after two creates: no cleanup and no receipt can run.
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(WORKLOADS)!r})\n"
            "import attachment_copy, attachment_stores\n"
            "create, count = attachment_stores.DestinationStore.create, [0]\n"
            "def dying(self, key, content):\n"
            "    create(self, key, content)\n"
            "    count[0] += 1\n"
            "    if count[0] == 2:\n"
            "        os._exit(137)\n"
            "attachment_stores.DestinationStore.create = dying\n"
            "attachment_copy.main()\n")
        killed = subprocess.run([sys.executable, "-c", script], check=False, env=self.environment(),
                                capture_output=True, text=True, timeout=300)
        self.assertEqual((killed.returncode, killed.stdout), (137, ""))
        self.assertEqual(len(contents(self.target)), 2)
        receipt = self.assert_copied()
        self.assertEqual((receipt["copied"], receipt["reused"]), (2, 2))
        self.assertEqual(contents(self.target), objects)


class FailureOutputTests(Case):
    def test_wrong_destination_secret_is_reported_without_values(self):
        self.seed({KEY + "a": BODY})
        code, receipt = self.command(ATTACHMENT_TARGET_SECRET_ACCESS_KEY="leak-marker-wrong-secret")
        self.assertEqual(code, 1)
        self.assertEqual(receipt["stage"], "transfer")
        self.assertIn(receipt["s3_error_code"], {"SignatureDoesNotMatch", "AccessDenied"})
        self.assertEqual(contents(self.target), {})

    def test_unreachable_destination_is_reported_without_values(self):
        self.seed({KEY + "a": BODY})
        code, receipt = self.command(ATTACHMENT_TARGET_ENDPOINT_URL="http://127.0.0.1:1")
        self.assertEqual(code, 1)
        self.assertEqual((receipt["stage"], receipt["reason"]), ("transfer", "unclassified"))
        self.assertEqual(receipt["error_category"], "EndpointConnectionError")

    def test_unreachable_database_is_reported_without_values(self):
        self.seed({KEY + "a": BODY})
        code, receipt = self.command(PGPORT="1", PGPASSWORD="leak-marker-password")
        self.assertEqual(code, 1)
        self.assertEqual((receipt["stage"], receipt["error_category"]), ("database_inventory", "OperationalError"))
        self.assertEqual(contents(self.target), {})

    def test_missing_table_is_reported_without_values(self):
        self.seed({KEY + "a": BODY})
        code, receipt = self.command(PGDATABASE="postgres")
        self.assertEqual(code, 1)
        self.assertEqual((receipt["stage"], receipt["error_category"]), ("database_inventory", "UndefinedTable"))


    def test_remote_database_without_verify_full_is_refused(self):
        # hostaddr reaches the local container while libpq still treats the name as the remote host.
        self.seed({KEY + "a": BODY})
        receipt = self.assert_rejected("database connection must use verify-full TLS",
                                       PGHOST="source-db.invalid", PGHOSTADDR="127.0.0.1", PGSSLMODE="disable")
        self.assertEqual(receipt["stage"], "database_inventory")
        self.assertEqual(contents(self.target), {})

    def test_loopback_database_without_the_test_opt_in_is_refused(self):
        self.seed({KEY + "a": BODY})
        receipt = self.assert_rejected("database connection must use verify-full TLS",
                                       ATTACHMENT_DB_ALLOW_LOOPBACK_PLAINTEXT="")
        self.assertEqual(receipt["stage"], "database_inventory")
        self.assertEqual(contents(self.target), {})


class VerifiedDatabaseTlsTests(Case):
    def over_tls(self, host_name):
        # hostaddr reaches the local container; libpq verifies the certificate against the name.
        with tempfile.TemporaryDirectory() as directory:
            authority = Path(directory) / "ca.crt"
            authority.write_text(ENV["database_ca"])
            return self.command(PGHOST=host_name, PGHOSTADDR="127.0.0.1", PGSSLMODE="verify-full",
                                PGSSLROOTCERT=str(authority))

    def test_verify_full_connection_is_accepted(self):
        self.seed({KEY + "a": BODY})
        code, receipt = self.over_tls(ENV["database_tls_name"])
        self.assertEqual((code, receipt["copied"]), (0, 1))

    def test_certificate_for_another_name_is_refused(self):
        self.seed({KEY + "a": BODY})
        code, receipt = self.over_tls("other-db.invalid")
        self.assertEqual(code, 1)
        self.assertEqual((receipt["stage"], receipt["error_category"]), ("database_inventory", "OperationalError"))
        self.assertEqual(contents(self.target), {})


class ReadOnlyBoundaryTests(Case):
    def test_source_store_has_no_write_surface(self):
        store = self.source_store()
        public = {name for name in dir(store) if not name.startswith("_")}
        self.assertEqual(public, {"list_keys", "read", "sizes"})

    def test_database_connection_refuses_writes(self):
        with attachment_copy.connect_read_only(dsn(), allow_loopback_plaintext=True) as connection, self.assertRaises(
                psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("INSERT INTO attachments (id, object_key, filename, content_type, size)"
                               " VALUES (%s, 'x', 'x', 'x', 0)", (uuid.uuid4(),))

    def test_source_ignores_an_endpoint_from_the_environment(self):
        ambient = {"AWS_ENDPOINT_URL": ENV["target_endpoint"], "AWS_ENDPOINT_URL_S3": ENV["target_endpoint"]}
        with patch.dict(os.environ, ambient):
            store = attachment_stores.source_store(BUCKET, "us-east-1", expected_owner="123456789012")
        self.assertNotIn(ENV["target_endpoint"], store._client.meta.endpoint_url)
        self.assertIn("amazonaws.com", store._client.meta.endpoint_url)

    def test_inventory_read_blocked_by_a_lock_times_out(self):
        holder = admin_database()
        self.addCleanup(holder.close)
        holder.execute("BEGIN")
        holder.execute("LOCK TABLE attachments IN ACCESS EXCLUSIVE MODE")
        # Releases the lock later, so a missing timeout fails this test instead of hanging the suite.
        release = threading.Timer(3, holder.close)
        release.start()
        self.addCleanup(release.cancel)
        with patch.object(attachment_copy, "STATEMENT_TIMEOUT_MS", 200), \
                attachment_copy.connect_read_only(dsn(), allow_loopback_plaintext=True) as connection, \
                self.assertRaises(psycopg.errors.QueryCanceled):
            attachment_copy.read_referenced_keys(connection)

    def test_destination_signs_only_with_its_own_keys(self):
        ambient = {"AWS_ACCESS_KEY_ID": "ambient-key", "AWS_SECRET_ACCESS_KEY": "ambient-secret",
                   "AWS_SESSION_TOKEN": "ambient-token"}
        seen = []
        with patch.dict(os.environ, ambient):
            store = self.target_store()
            store._client.meta.events.register("before-send.s3", lambda request, **_: seen.append(request.headers))
            store.list_keys()
        (headers,) = seen
        self.assertIn(ENV["target_client"][0], headers["Authorization"].decode())
        self.assertNotIn("X-Amz-Security-Token", headers)

    def test_cut_stream_is_not_taken_for_the_whole_object(self):
        class CutClient:
            def get_object(self, **request):
                return {"Body": io.BytesIO(b"partial"), "ContentLength": 1024}

        with self.assertRaisesRegex(ValueError, "incomplete object read"):
            attachment_stores.SourceStore(CutClient(), BUCKET).read(KEY)

    def test_expected_owner_is_sent_with_every_source_request(self):
        from botocore.exceptions import ClientError
        from botocore.stub import Stubber
        store = attachment_stores.source_store(BUCKET, "us-east-1", expected_owner="123456789012",
                                               endpoint_url=ENV["source_endpoint"])
        with Stubber(store._client) as stubber:
            stubber.add_response("list_objects_v2", {"IsTruncated": False}, {
                "Bucket": BUCKET, "MaxKeys": attachment_copy.MAX_OBJECTS + 1,
                "ExpectedBucketOwner": "123456789012"})
            stubber.add_client_error("get_object", "AccessDenied", expected_params={
                "Bucket": BUCKET, "Key": KEY, "ExpectedBucketOwner": "123456789012"})
            self.assertEqual(store.list_keys(), [])
            with self.assertRaises(ClientError):
                store.read(KEY)
            stubber.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
