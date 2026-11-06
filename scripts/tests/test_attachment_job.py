"""The rendered copy Job must satisfy what the copy code, the object store and the DB route require."""
import importlib
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
WORKLOADS = Path(os.environ.get("T8_ATTACH_MODULE_DIR") or REPOSITORY / "infra/envs/target-workloads")
# Other environments also have a module called render; never let this test or them see the wrong one.
with patch.object(sys, "path", [str(WORKLOADS), *sys.path]), patch.dict(sys.modules):
    for shadowed in ("render", "replication", "objects"):
        sys.modules.pop(shadowed, None)
    attachment_job = importlib.import_module("attachment_job")
    attachment_copy = importlib.import_module("attachment_copy")
    objects = importlib.import_module("objects")
    replication = importlib.import_module("replication")

DIGEST = "sha256:" + "a" * 64
IMAGE = "registry.example.invalid/bravo/attachment-copy@" + DIGEST
RUN_ID = "demo-2026-11-07-r1"
PLAN_ID = "Plan_2026-11-07"
EPOCH = 3
ADDRESS = "10.43.0.25"
SERVICE_UID = "0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f"


def render(**overrides):
    arguments = {"image": IMAGE, "run_id": RUN_ID, "plan_id": PLAN_ID, "epoch": EPOCH, "egress_address": ADDRESS,
                 "egress_service_uid": SERVICE_UID, **overrides}
    return attachment_job.job(**arguments)


def pod_spec(job=None):
    return (job or render())["spec"]["template"]["spec"]


def container(job=None):
    return pod_spec(job)["containers"][0]


def env_of(job=None):
    return {item["name"]: item for item in container(job)["env"]}


def as_process_environ(job=None):
    """What the container sees, with an obviously synthetic value per secret reference."""
    environ = {}
    for name, item in env_of(job).items():
        if "value" in item:
            environ[name] = item["value"]
        else:
            reference = item["valueFrom"]["secretKeyRef"]
            environ[name] = f"synthetic-{reference['name']}-{reference['key']}"
    return environ


class ExecutionShape(unittest.TestCase):
    def test_job_is_named_after_the_run_in_the_target_namespace(self):
        job = render()
        self.assertEqual((job["apiVersion"], job["kind"]), ("batch/v1", "Job"))
        self.assertEqual(job["metadata"]["namespace"], "target-dev")
        self.assertEqual(job["metadata"]["name"], "attachment-copy-" + RUN_ID)
        self.assertEqual(job["metadata"]["labels"][attachment_job.RUN_LABEL], RUN_ID)
        self.assertEqual(job["metadata"]["labels"]["component"], "target")

    def test_one_attempt_only_and_the_result_stays_readable_for_a_while(self):
        spec = render()["spec"]
        self.assertEqual((spec["backoffLimit"], spec["parallelism"], spec["completions"]), (0, 1, 1))
        self.assertEqual(spec["template"]["spec"]["restartPolicy"], "Never")
        self.assertGreater(spec["activeDeadlineSeconds"], 0)
        # The receipt must still be readable an hour after the run; rerunning covers anything later.
        self.assertGreaterEqual(spec["ttlSecondsAfterFinished"], 3600)

    def test_deadline_ends_before_the_issued_source_session(self):
        self.assertLess(render()["spec"]["activeDeadlineSeconds"], attachment_job.SOURCE_SESSION_SECONDS)

    def test_restricted_pod_security(self):
        pod = pod_spec()
        security = pod["securityContext"]
        self.assertIs(security["runAsNonRoot"], True)
        self.assertGreater(security["runAsUser"], 0)
        self.assertGreater(security["runAsGroup"], 0)
        self.assertEqual(security["seccompProfile"], {"type": "RuntimeDefault"})
        for own in (container(), pod["initContainers"][0]):
            self.assertIs(own["securityContext"]["allowPrivilegeEscalation"], False)
            self.assertIs(own["securityContext"]["readOnlyRootFilesystem"], True)
            self.assertEqual(own["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertIs(pod["automountServiceAccountToken"], False)
        # The tokenless account from render.py accounts, not the namespace default (CIS 5.1.5, 5.1.6).
        self.assertEqual(pod["serviceAccountName"], "attachment-copy")
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            self.assertNotIn(field, pod)
        self.assertFalse([v for v in pod["volumes"] if "hostPath" in v])

    def test_bounded_resources_above_the_measured_peak(self):
        limits = container()["resources"]["limits"]
        self.assertEqual(limits["memory"], "256Mi")  # 108MB peak was measured with 10MiB + 6MiB.
        self.assertIn("cpu", limits)
        self.assertIn("requests", container()["resources"])

    def test_copy_starts_only_after_the_object_store_route_opens(self):
        init = pod_spec()["initContainers"]
        self.assertEqual([item["name"] for item in init], ["wait-for-target"])
        self.assertEqual(init[0]["image"], IMAGE)
        self.assertIn("resources", init[0])
        self.assertEqual(init[0]["command"][:2], ["python3", "-c"])
        script = init[0]["command"][2]
        self.assertIn(f"({attachment_job.TARGET_HOST!r}, {attachment_job.TARGET_PORT})", script)
        compile(script, "wait-for-target", "exec")

    def test_wait_gives_up_well_before_the_deadline(self):
        script = pod_spec()["initContainers"][0]["command"][2]
        attempts = int(re.search(r"range\((\d+)\)", script).group(1))
        pause = int(re.search(r"sleep\((\d+)\)", script).group(1))
        connect = int(re.search(r"timeout=(\d+)", script).group(1))
        self.assertLess(attempts * (pause + connect), render()["spec"]["activeDeadlineSeconds"] / 2)

    def test_only_the_sanitized_receipt_reaches_the_log(self):
        self.assertEqual(container()["command"],
                         ["/bin/sh", "-c", "exec python3 /app/attachment_copy.py 2>/dev/null"])


class ImageAndIdentifiers(unittest.TestCase):
    def test_digest_pinned_image_is_accepted(self):
        for image in (IMAGE, "localhost/attachment-copy:v1@" + DIGEST):
            self.assertEqual(container(render(image=image))["image"], image)

    def test_anything_without_an_exact_digest_is_refused(self):
        for image in ("attachment-copy", "attachment-copy:latest", "attachment-copy@sha256:abc",
                      "attachment-copy@sha256:" + "A" * 64, "attachment-copy@sha512:" + "a" * 64,
                      " attachment-copy@" + DIGEST, "attachment-copy@" + DIGEST + "\n",
                      "@" + DIGEST, "--help@" + DIGEST, None):
            with self.subTest(image=image), self.assertRaises(ValueError):
                render(image=image)

    def test_run_id_becomes_a_valid_job_name_and_label(self):
        longest = "a" * attachment_job.MAX_RUN_ID_LENGTH
        job = render(run_id=longest)
        self.assertLessEqual(len(job["metadata"]["name"]), 63)
        for run_id in ("", "UPPER", "under_score", "-leading", "trailing-", "a" * (attachment_job.MAX_RUN_ID_LENGTH + 1),
                       "dot.ted", "line\n", None):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                render(run_id=run_id)

    def test_only_a_private_ipv4_egress_address_is_accepted(self):
        for address in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "0.0.0.0", "::1", "not-an-address", None):
            with self.subTest(address=address), self.assertRaises(ValueError):
                render(egress_address=address)

    def test_plan_and_epoch_use_the_executor_contract_and_are_recorded(self):
        annotations = render()["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations[attachment_job.PLAN_ANNOTATION], PLAN_ID)
        self.assertEqual(annotations[attachment_job.EPOCH_ANNOTATION], str(EPOCH))
        self.assertEqual(render(plan_id="A" * 128)["kind"], "Job")
        for plan_id in ("", "-leading", "has space", "A" * 129, "line\n", None):
            with self.subTest(plan_id=plan_id), self.assertRaises(ValueError):
                render(plan_id=plan_id)
        for epoch in (0, -1, "1", 1.0, True, None):
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                render(epoch=epoch)

    def test_exact_service_uid_is_required_and_recorded(self):
        annotations = render()["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations[attachment_job.EGRESS_UID_ANNOTATION], SERVICE_UID)
        for uid in ("", "not-a-uid", SERVICE_UID.upper(), SERVICE_UID.replace("-", ""), None):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                render(egress_service_uid=uid)


EXPECTED_REFERENCES = {
    "ATTACHMENT_SOURCE_BUCKET": ("attachment-copy-source", "bucket"),
    "ATTACHMENT_SOURCE_REGION": ("attachment-copy-source", "region"),
    "ATTACHMENT_SOURCE_BUCKET_OWNER": ("attachment-copy-source", "bucket_owner"),
    "AWS_ACCESS_KEY_ID": ("attachment-copy-source", "aws_access_key_id"),
    "AWS_SECRET_ACCESS_KEY": ("attachment-copy-source", "aws_secret_access_key"),
    "AWS_SESSION_TOKEN": ("attachment-copy-source", "aws_session_token"),
    "ATTACHMENT_TARGET_ACCESS_KEY_ID": ("target-object-client", "AWS_ACCESS_KEY_ID"),
    "ATTACHMENT_TARGET_SECRET_ACCESS_KEY": ("target-object-client", "AWS_SECRET_ACCESS_KEY"),
    "PGPASSWORD": ("t8-source-replication", "password"),
}
EXPECTED_VALUES = {
    "ATTACHMENT_TARGET_ENDPOINT_URL", "ATTACHMENT_TARGET_BUCKET",
    "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGSSLMODE", "PGSSLROOTCERT", "PGAPPNAME",
}


class SecretsByNameOnly(unittest.TestCase):
    def test_every_credential_and_private_identifier_is_a_secret_reference(self):
        found = {name: (item["valueFrom"]["secretKeyRef"]["name"], item["valueFrom"]["secretKeyRef"]["key"])
                 for name, item in env_of().items() if "valueFrom" in item}
        self.assertEqual(found, EXPECTED_REFERENCES)

    def test_plain_values_are_only_the_public_route_contract(self):
        self.assertEqual({name for name, item in env_of().items() if "value" in item}, EXPECTED_VALUES)

    def test_session_token_is_mandatory_so_a_long_lived_key_alone_cannot_run(self):
        self.assertNotIn("optional", env_of()["AWS_SESSION_TOKEN"]["valueFrom"]["secretKeyRef"])

    def test_no_test_only_switch_is_rendered(self):
        for name in ("ATTACHMENT_SOURCE_ENDPOINT_URL", "ATTACHMENT_DB_ALLOW_LOOPBACK_PLAINTEXT"):
            self.assertNotIn(name, env_of())


class Consumers(unittest.TestCase):
    """Bind to the code that reads this manifest rather than repeating its names here."""

    def test_copy_code_finds_all_of_its_configuration(self):
        calls = []
        stores = SimpleNamespace(source_store=lambda *a: calls.append(("source", a)) or "source",
                                 destination_store=lambda *a: calls.append(("target", a)) or "target")
        with patch.dict(sys.modules, {"attachment_stores": stores}):
            self.assertEqual(attachment_copy.stores_from(as_process_environ()), ("source", "target"))
        source_arguments = dict(calls)["source"]
        self.assertEqual(source_arguments[3], None)  # Real S3: the SDK resolves the endpoint itself.
        self.assertEqual(dict(calls)["target"][0], attachment_job.TARGET_ENDPOINT)

    def test_copy_code_accepts_the_database_settings_before_any_password_leaves(self):
        settings = attachment_copy.tls_settings({}, as_process_environ())
        self.assertEqual(settings, {"sslmode": "verify-full", "gssencmode": "disable"})

    def test_database_route_is_the_existing_replication_contract(self):
        contract = replication.external_cluster("target", ADDRESS)
        parameters = contract["connectionParameters"]
        environ = as_process_environ()
        for variable, field in (("PGHOST", "host"), ("PGHOSTADDR", "hostaddr"), ("PGPORT", "port"),
                                ("PGDATABASE", "dbname"), ("PGUSER", "user"), ("PGSSLMODE", "sslmode")):
            self.assertEqual(environ[variable], parameters[field], variable)
        password = env_of()["PGPASSWORD"]["valueFrom"]["secretKeyRef"]
        self.assertEqual(password, contract["password"])

    def test_ca_comes_from_the_replication_secret_and_is_mounted_where_libpq_looks(self):
        contract = replication.external_cluster("target", ADDRESS)["sslRootCert"]
        volume = next(v for v in pod_spec()["volumes"] if v.get("secret", {}).get("secretName") == contract["name"])
        self.assertEqual(volume["secret"]["items"], [{"key": contract["key"], "path": contract["key"]}])
        mount = next(m for m in container()["volumeMounts"] if m["name"] == volume["name"])
        self.assertIs(mount["readOnly"], True)
        self.assertEqual(as_process_environ()["PGSSLROOTCERT"], f"{mount['mountPath']}/{contract['key']}")

    def test_object_store_network_policy_admits_this_pod(self):
        store = objects.objects("t8-node")
        policy = next(item for item in store if item["kind"] == "NetworkPolicy")
        required = policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
        labels = render()["spec"]["template"]["metadata"]["labels"]
        self.assertEqual({key: labels.get(key) for key in required}, required)

    def test_object_store_address_matches_its_service(self):
        store = objects.objects("t8-node")
        service = next(item for item in store if item["kind"] == "Service")
        self.assertEqual(attachment_job.TARGET_ENDPOINT,
                         f"http://{service['metadata']['name']}.{service['metadata']['namespace']}"
                         f".svc.cluster.local:{service['spec']['ports'][0]['port']}")
        self.assertEqual(as_process_environ()["ATTACHMENT_TARGET_BUCKET"], "demo-chat-attachments")


class ApprovedExtras(unittest.TestCase):
    def test_default_mounts_no_extras_so_unknown_source_objects_still_fail(self):
        self.assertNotIn("ATTACHMENT_APPROVED_EXTRA_KEYS_FILE", env_of())
        self.assertFalse([v for v in pod_spec()["volumes"] if v["name"] == "approved-extras"])

    def test_explicit_extras_come_from_a_read_only_secret_file(self):
        job = render(with_approved_extras=True)
        path = as_process_environ(job)["ATTACHMENT_APPROVED_EXTRA_KEYS_FILE"]
        volume = next(v for v in pod_spec(job)["volumes"] if v["name"] == "approved-extras")
        self.assertEqual(volume["secret"]["secretName"], "attachment-copy-approved-extras")
        mount = next(m for m in container(job)["volumeMounts"] if m["name"] == "approved-extras")
        self.assertIs(mount["readOnly"], True)
        self.assertEqual(path, f"{mount['mountPath']}/{volume['secret']['items'][0]['path']}")


BUCKET_ARN = "arn:aws:s3:::example-attachments-123456789012"
KMS_ARN = "arn:aws:kms:ap-northeast-2:123456789012:key/0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f"


class SourceSessionPolicy(unittest.TestCase):
    def policy(self):
        return attachment_job.source_session_policy(BUCKET_ARN, KMS_ARN)

    def statement(self, effect, action):
        return next(s for s in self.policy()["Statement"] if s["Effect"] == effect and s.get("Action") == action)

    def test_grants_only_list_read_and_decrypt_through_s3(self):
        allowed = [s for s in self.policy()["Statement"] if s["Effect"] == "Allow"]
        self.assertEqual({s["Action"]: s["Resource"] for s in allowed},
                         {"s3:ListBucket": BUCKET_ARN, "s3:GetObject": BUCKET_ARN + "/*", "kms:Decrypt": KMS_ARN})
        self.assertEqual(self.statement("Allow", "kms:Decrypt")["Condition"],
                         {"StringEquals": {"kms:ViaService": "s3.ap-northeast-2.amazonaws.com"}})

    def test_explicit_denial_also_limits_grants_from_resource_policies(self):
        deny = [s for s in self.policy()["Statement"] if s["Effect"] == "Deny"]
        not_action = next(s for s in deny if "NotAction" in s)
        self.assertEqual(set(not_action["NotAction"]), {"s3:ListBucket", "s3:GetObject", "kms:Decrypt"})
        self.assertEqual(not_action["Resource"], "*")
        other_bucket = next(s for s in deny if "NotResource" in s)
        self.assertEqual(set(other_bucket["Action"]), {"s3:ListBucket", "s3:GetObject"})
        self.assertEqual(set(other_bucket["NotResource"]), {BUCKET_ARN, BUCKET_ARN + "/*"})

    def test_fits_the_session_policy_size_limit(self):
        # AssumeRole and GetFederationToken share the 2,048-character limit for an inline session policy.
        self.assertLess(len(json.dumps(self.policy(), separators=(",", ":"))), 2048)

    def test_session_fits_the_role_chaining_limit(self):
        # Assuming the reader role from an SSO session is role chaining, which AWS caps at one hour.
        self.assertLessEqual(attachment_job.SOURCE_SESSION_SECONDS, 3600)

    def test_reader_role_policy_is_the_same_document(self):
        # infra/envs/attachment-copy-reader compares its role policy with this fixture for these inputs.
        fixture = REPOSITORY / "infra/envs/attachment-copy-reader/tests/session-policy.json"
        bucket = "arn:aws:s3:::example-attachments-111122223333"
        key = "arn:aws:kms:ap-northeast-2:111122223333:key/0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f"
        self.assertEqual(attachment_job.source_session_policy(bucket, key), json.loads(fixture.read_text()))

    def test_wildcards_and_other_shapes_are_refused(self):
        for bucket, key in (("arn:aws:s3:::*-123456789012", KMS_ARN), (BUCKET_ARN + "/*", KMS_ARN), ("bucket", KMS_ARN),
                            (BUCKET_ARN, "*"), (BUCKET_ARN, KMS_ARN.replace("key/", "alias/")), (None, KMS_ARN)):
            with self.subTest(bucket=bucket, key=key), self.assertRaises(ValueError):
                attachment_job.source_session_policy(bucket, key)


class Packaging(unittest.TestCase):
    DOCKERFILE = (WORKLOADS / "attachment-copy.Dockerfile")
    LOCK = (WORKLOADS / "attachment-copy.requirements.txt")

    def test_base_is_digest_pinned_and_the_user_matches_the_pod(self):
        text = self.DOCKERFILE.read_text()
        self.assertRegex(text, r"(?m)^FROM \S+@sha256:[0-9a-f]{64}$")
        security = pod_spec()["securityContext"]
        self.assertIn(f"USER {security['runAsUser']}:{security['runAsGroup']}", text)

    def test_non_root_user_can_enter_the_application_directory(self):
        # COPY --chmod=0444 alone creates /app without the search bit; the real image then fails to import.
        text = self.DOCKERFILE.read_text()
        self.assertLess(text.index("WORKDIR /app"), text.index("COPY --chmod=0444"))

    def test_image_carries_the_three_copy_modules_and_no_secret_setting(self):
        text = self.DOCKERFILE.read_text()
        for module in ("attachment_copy.py", "attachment_stores.py", "sync_attachments.py"):
            self.assertIn(module, text)
        self.assertNotRegex(text, r"(?m)^(ENV|ARG)\b.*(AWS_|PGPASSWORD|SECRET|TOKEN)")

    def test_dependencies_are_hash_locked_at_the_versions_the_script_declares(self):
        header = (WORKLOADS / "attachment_copy.py").read_text()
        declared = re.search(r"dependencies = \[(.*)\]", header).group(1)
        lock = self.LOCK.read_text()
        self.assertIn("--require-hashes", self.DOCKERFILE.read_text())
        for pin in re.findall(r'"([^"]+)"', declared):
            name, version = re.sub(r"\[.*\]", "", pin).split("==")
            self.assertRegex(lock, rf"(?m)^{re.escape(name)}(\[\w+\])?=={re.escape(version)} \\$")
        self.assertRegex(lock, r"(?m)^psycopg-binary==3\.3\.6 ")
        self.assertNotRegex(lock, r"(?m)^[a-z0-9_.-]+(\[\w+\])?==[^\s]+$")  # A pin with no hash line.


class OperatorGuide(unittest.TestCase):
    """The k3s run executes this recipe. These checks catch a drift without needing a cluster."""

    GUIDE = REPOSITORY / "docs/operations/t8-attachment-copy-job.md"

    def recipe(self, marker="source-secret-recipe"):
        return re.search(rf"<!-- {marker} -->\s*```sh\n(.*?)```", self.GUIDE.read_text(), re.DOTALL).group(1)

    def recipes(self):
        """The default for SSO users, then the IAM user variant."""
        return {"assume-role": self.recipe(), "federation": self.recipe("source-secret-recipe-iam-user")}

    @staticmethod
    def session_command(recipe):
        return recipe[:recipe.index("| jq")].strip()

    def test_secret_recipe_writes_every_key_the_job_reads(self):
        read = {item["valueFrom"]["secretKeyRef"]["key"] for item in env_of().values()
                if item.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == attachment_job.SOURCE_SECRET}
        self.assertEqual(set(re.findall(r'"([a-z_]+)=\\\(', self.recipe())), read)
        self.assertIn(f"secret generic {attachment_job.SOURCE_SECRET} ", self.recipe())

    def test_default_session_assumes_the_reader_role(self):
        command = self.session_command(self.recipe())
        self.assertTrue(command.startswith('aws sts assume-role --role-arn "$SOURCE_READER_ROLE_ARN" '))
        self.assertIn(f" --role-session-name {attachment_job.SOURCE_SESSION_NAME} ", command)

    def test_iam_user_variant_requests_a_federation_token(self):
        command = self.session_command(self.recipes()["federation"])
        self.assertTrue(command.startswith(f"aws sts get-federation-token --name {attachment_job.SOURCE_SESSION_NAME} "))

    def test_both_sessions_carry_the_job_session_length_and_policy(self):
        for name, recipe in self.recipes().items():
            with self.subTest(name):
                command = self.session_command(recipe)
                self.assertEqual(int(re.search(r"--duration-seconds (\d+)", command).group(1)),
                                 attachment_job.SOURCE_SESSION_SECONDS)
                self.assertIn(' --policy "$SESSION_POLICY" ', command)

    def test_variants_differ_only_in_the_session_command(self):
        # The k3s run executes the default pipeline; the IAM user variant must be the same pipeline.
        tails = {recipe[recipe.index("| jq"):] for recipe in self.recipes().values()}
        self.assertEqual(len(tails), 1)


class CommandLine(unittest.TestCase):
    def run_renderer(self, *arguments):
        return subprocess.run([sys.executable, str(WORKLOADS / "attachment_job.py"), *arguments],
                              capture_output=True, text=True, check=False, timeout=30)

    def job_arguments(self, image=IMAGE):
        return ["job", "--image", image, "--run-id", RUN_ID, "--plan-id", PLAN_ID, "--epoch", str(EPOCH),
                "--egress-address", ADDRESS, "--egress-service-uid", SERVICE_UID]

    def test_job_stage_prints_one_list_with_one_job(self):
        done = self.run_renderer(*self.job_arguments())
        self.assertEqual(done.returncode, 0, done.stderr)
        listing = json.loads(done.stdout)
        self.assertEqual((listing["kind"], [item["kind"] for item in listing["items"]]), ("List", ["Job"]))

    def test_extras_flag_reaches_the_manifest(self):
        done = self.run_renderer(*self.job_arguments(), "--with-approved-extras")
        self.assertIn("attachment-copy-approved-extras", done.stdout)

    def test_policy_stage_prints_one_compact_line(self):
        done = self.run_renderer("source-policy", "--bucket-arn", BUCKET_ARN, "--kms-key-arn", KMS_ARN)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.count("\n"), 1)
        self.assertEqual(json.loads(done.stdout), attachment_job.source_session_policy(BUCKET_ARN, KMS_ARN))

    def test_invalid_input_is_a_usage_error_without_a_traceback(self):
        done = self.run_renderer(*self.job_arguments("attachment-copy:latest"))
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, "")
        self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
