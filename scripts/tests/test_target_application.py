"""T30 target application render contract and its local Secrets Manager answer."""
import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = ROOT / "infra/envs/target-workloads"
PIN = json.loads((WORKLOADS / "app-image.json").read_text())
GHCR = "ghcr.io/softbank-hackathon-finals/demo-chat"
ECR = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat"
IMAGE = f"{GHCR}@{PIN['index_digest']}"
ECR_IMAGE = f"{ECR}@{PIN['index_digest']}"
# The same source rebuilt locally gets another digest. It must not pass as the registered image.
LOCAL_BUILD = "sha256:c3ee149d045b8875e7c4ddc5bab609e623e219929362cf4d20331a271d5cff9e"


def load(path, name):
    # Several environments ship a module called render; keep this one out of sys.modules.
    with patch.dict(sys.modules), patch.object(sys, "path", [str(path.parent), *sys.path]):
        sys.modules.pop("render", None)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


application = load(WORKLOADS / "application.py", "target_application")
compat = load(WORKLOADS / "secrets_manager_compat.py", "secrets_manager_compat")
target_secrets = load(ROOT / "infra/envs/target-secrets/render.py", "target_secrets_render")


def rendered(**kwargs):
    items = application.application(kwargs.pop("image", IMAGE), **kwargs)
    return {item["kind"]: item for item in items}


def pod_spec(**kwargs):
    return rendered(**kwargs)["Deployment"]["spec"]["template"]["spec"]


def app_container(**kwargs):
    return pod_spec(**kwargs)["containers"][0]


def sidecar(**kwargs):
    return pod_spec(**kwargs)["initContainers"][0]


def env_values(container):
    return {e["name"]: e["value"] for e in container["env"] if "value" in e}


def env_refs(container):
    return {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"] if "valueFrom" in e}


class RenderContractTests(unittest.TestCase):
    def test_renders_only_script_deployment_and_service(self):
        items = application.application(IMAGE)
        self.assertEqual(sorted(i["kind"] for i in items), ["ConfigMap", "Deployment", "Service"])
        for item in items:
            self.assertEqual(item["metadata"]["namespace"], "target-dev")
            self.assertEqual(item["metadata"]["labels"]["component"], "target")

    def test_names_match_source_and_other_tasks_contracts(self):
        objects = rendered()
        self.assertEqual(objects["Deployment"]["metadata"]["name"], "demo-chat")
        self.assertEqual(objects["Service"]["metadata"]["name"], "demo-chat")
        self.assertEqual(app_container()["name"], "app")

    def test_service_is_cluster_internal_and_selects_the_pod(self):
        objects = rendered()
        spec = objects["Service"]["spec"]
        self.assertEqual(spec["type"], "ClusterIP")
        self.assertEqual(spec["ports"], [{"name": "http", "port": 8080, "targetPort": 8080}])
        labels = objects["Deployment"]["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(spec["selector"], {"app": "demo-chat"})
        self.assertEqual(objects["Deployment"]["spec"]["selector"]["matchLabels"], spec["selector"])
        self.assertTrue(set(spec["selector"].items()) <= set(labels.items()))

    def test_environment_header_uses_the_registered_target_value(self):
        # X-Bravo-Env is APP_ENV, and T18/T37 register the target as onprem.
        self.assertEqual(env_values(app_container())["APP_ENV"], "onprem")

    def test_starts_write_blocked_and_reaches_db_only_through_target_pooler(self):
        values = env_values(app_container())
        self.assertEqual(values["WRITE_BLOCKED"], "true")
        self.assertEqual(values["DB_HOST"], "target-pgbouncer.target-dev.svc.cluster.local")
        self.assertEqual((values["DB_PORT"], values["DB_NAME"]), ("5432", "chat"))
        self.assertEqual((values["PGSSLMODE"], values["PGSSLROOTCERT"]),
                         ("verify-full", "/etc/cnpg-ca/ca.crt"))

    def test_target_refuses_attachment_uploads_before_cutover(self):
        # The object would land in target storage while its row goes to the source DB through the pooler.
        self.assertEqual(env_values(app_container())["ATTACHMENT_UPLOAD_ENABLED"], "false")

    def test_probes_do_not_depend_on_the_database(self):
        container = app_container()
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/readyz", "port": 8080})
        self.assertEqual(container["livenessProbe"]["httpGet"], {"path": "/healthz", "port": 8080})

    def test_slow_development_vm_cannot_kill_a_starting_app(self):
        # The VM emulates amd64, so liveness must wait until the app has answered once.
        probe = app_container()["startupProbe"]
        self.assertEqual(probe["httpGet"], {"path": "/healthz", "port": 8080})
        self.assertGreaterEqual(probe["periodSeconds"] * probe["failureThreshold"], 300)

    def test_app_keeps_serving_until_the_service_stops_sending_traffic(self):
        # Measured on k3s: 3 to 8 of 12 requests right after a rollout failed without this, none with it.
        self.assertGreaterEqual(app_container()["lifecycle"]["preStop"]["sleep"]["seconds"], 5)

    def test_sidecar_startup_check_does_not_start_python(self):
        # Under the CPU limit an interpreter start overran the probe timeout on a fast machine.
        probe = sidecar()["startupProbe"]
        self.assertEqual(probe["exec"]["command"],
                         ["/bin/bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{compat.PORT}"])
        self.assertGreaterEqual(probe["timeoutSeconds"], 5)
        self.assertGreaterEqual(probe["periodSeconds"] * probe["failureThreshold"], 120)

    def test_secret_values_never_appear_and_consumers_use_t9_contract(self):
        objects = rendered()
        self.assertNotIn("Secret", objects)
        refs = env_refs(app_container())
        self.assertEqual(refs, {
            "AWS_ACCESS_KEY_ID": {"name": "target-app-objects", "key": "access_key"},
            "AWS_SECRET_ACCESS_KEY": {"name": "target-app-objects", "key": "secret_key"},
            "ADMIN_TOKEN": {"name": "target-app-admin", "key": "token"},
        })
        plain = env_values(app_container())
        for name in plain:
            self.assertNotRegex(name, "PASSWORD|TOKEN|SECRET_ACCESS|ACCESS_KEY")
        delivered = {}
        for item in target_secrets.delivery()["items"]:
            if item["kind"] == "ExternalSecret":
                delivered[item["metadata"]["name"]] = {d["secretKey"] for d in item["spec"]["data"]}
        for ref in refs.values():
            self.assertIn(ref["key"], delivered[ref["name"]])
        volumes = {v["name"]: v for v in pod_spec()["volumes"]}
        database = volumes["database-credentials"]["secret"]
        self.assertEqual(database["items"], [{"key": "credentials.json", "path": "credentials.json"}])
        self.assertIn("credentials.json", delivered[database["secretName"]])
        self.assertEqual(plain["DB_SECRET_ID"], target_secrets.DB_PATH)

    def test_database_credentials_reach_only_the_sidecar(self):
        mounts = {m["name"] for m in app_container()["volumeMounts"]}
        self.assertNotIn("database-credentials", mounts)
        side = sidecar()
        by_name = {m["name"]: m for m in side["volumeMounts"]}
        self.assertEqual(by_name["database-credentials"]["mountPath"], "/run/bravo/database")
        self.assertTrue(by_name["database-credentials"]["readOnly"])
        self.assertEqual(env_refs(side), {})

    def test_only_the_public_ca_certificate_reaches_the_app(self):
        volumes = {v["name"]: v for v in pod_spec()["volumes"]}
        self.assertEqual(volumes["ca"]["secret"], {"secretName": "target-db-ca",
                                                   "items": [{"key": "ca.crt", "path": "ca.crt"}]})

    def test_credential_file_is_group_readable_only_by_the_pod_user(self):
        pod = pod_spec()
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["database-credentials"]["secret"]["defaultMode"], 0o440)
        self.assertEqual(pod["securityContext"]["fsGroup"], pod["securityContext"]["runAsUser"])

    def test_sidecar_runs_the_mounted_script_from_the_rendered_config_map(self):
        objects = rendered()
        side = sidecar()
        name = objects["ConfigMap"]["metadata"]["name"]
        script = next(iter(objects["ConfigMap"]["data"]))
        volumes = {v["name"]: v for v in pod_spec()["volumes"]}
        self.assertEqual(volumes["compat"]["configMap"]["name"], name)
        mount = next(m for m in side["volumeMounts"] if m["name"] == "compat")
        self.assertEqual(side["command"], ["/app/.venv/bin/python", f"{mount['mountPath']}/{script}"])

    def test_slow_responses_do_not_flap_the_pod_on_the_emulated_vm(self):
        container = app_container()
        for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
            with self.subTest(probe=probe):
                self.assertGreaterEqual(container[probe]["timeoutSeconds"], 5)

    def test_app_keeps_image_entrypoint_and_sidecar_is_separate(self):
        container = app_container()
        self.assertNotIn("command", container)
        self.assertNotIn("args", container)
        self.assertEqual(len(pod_spec()["containers"]), 1)
        side = sidecar()
        self.assertEqual(side["restartPolicy"], "Always")
        self.assertEqual(side["image"], IMAGE)
        self.assertEqual(env_values(container)["AWS_ENDPOINT_URL_SECRETS_MANAGER"],
                         f"http://127.0.0.1:{compat.PORT}")
        self.assertEqual(env_values(side)["DB_SECRET_ID"], env_values(container)["DB_SECRET_ID"])
        self.assertIn("exec", side["startupProbe"])

    def test_pod_declares_it_is_not_exit_evidence(self):
        metadata = rendered()["Deployment"]["spec"]["template"]["metadata"]
        self.assertEqual(metadata["annotations"]["bravo.t30/not-exit-evidence"],
                         "secrets-manager-compat-sidecar")

    def test_exit_drill_reads_the_same_marker_and_endpoint_variable(self):
        # The render and the Exit Drill collector are separate projects, so their CI runs can miss a rename.
        collector = (ROOT / "services/exit-drill/src/bravo_exit_drill/observation.py").read_text()
        metadata = rendered()["Deployment"]["spec"]["template"]["metadata"]
        for key in metadata["annotations"]:
            self.assertIn(f'NOT_EXIT_EVIDENCE_ANNOTATION = "{key}"', collector)
        self.assertIn('AWS_ENDPOINT_OVERRIDE = "AWS_ENDPOINT_URL"', collector)
        self.assertIn("AWS_ENDPOINT_URL_SECRETS_MANAGER", env_values(app_container()))

    def test_no_aws_endpoint_or_ambient_identity_is_configured(self):
        values = env_values(app_container())
        self.assertEqual(values["AWS_EC2_METADATA_DISABLED"], "true")
        self.assertEqual(values["S3_ENDPOINT_URL"], "http://seaweedfs.target-dev.svc.cluster.local:8333")
        self.assertEqual(values["S3_FORCE_PATH_STYLE"], "true")
        self.assertNotIn("amazonaws.com", json.dumps(values))
        self.assertFalse(pod_spec()["automountServiceAccountToken"])

    def test_object_store_policy_label_is_present(self):
        labels = rendered()["Deployment"]["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels["bravo-object-client"], "true")

    def test_every_container_meets_restricted_pod_security(self):
        pod = pod_spec()
        security = pod["securityContext"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertEqual(security["runAsUser"], 10001)
        self.assertEqual(security["seccompProfile"], {"type": "RuntimeDefault"})
        for container in [*pod["containers"], *pod["initContainers"]]:
            with self.subTest(container=container["name"]):
                self.assertEqual(container["securityContext"], {
                    "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]}})
                self.assertIn("limits", container["resources"])

    def test_script_in_config_map_is_the_reviewed_file(self):
        data = rendered()["ConfigMap"]["data"]
        self.assertEqual(data, {"secrets_manager_compat.py":
                                (WORKLOADS / "secrets_manager_compat.py").read_text()})


class ImageContractTests(unittest.TestCase):
    def test_accepts_exact_repositories_pinned_by_digest(self):
        for image in (IMAGE, ECR_IMAGE):
            with self.subTest(image=image):
                self.assertEqual(app_container(image=image)["image"], image)

    def test_rejects_tag_foreign_repository_and_lookalikes(self):
        for image in ("ghcr.io/softbank-hackathon-finals/demo-chat:latest",
                      "ghcr.io/softbank-hackathon-finals/demo-chat@sha256:abc",
                      "ghcr.io/softbank-hackathon-finals/demo-chat-other@sha256:" + "a" * 64,
                      "ghcr.io/other/demo-chat@sha256:" + "a" * 64,
                      "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/other@sha256:" + "a" * 64,
                      "evil.example/123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat@sha256:" + "a" * 64,
                      IMAGE + "\n", IMAGE + "@extra", None):
            with self.subTest(image=image), self.assertRaises(ValueError):
                application.application(image)

    def test_rejects_every_published_image_that_lacks_the_environment_header(self):
        # Index and amd64 digests from the receipts for ce5727e, 1a57a54 and 66e7419, all older than c1c74ab.
        for digest in ("001adc35a9fc767566552fb55c3c19a67b7c5efc7231fb02dc28cdeace99d0d4",
                       "bd1cd57a1c3dda9bb2795696167e242fc3b79a80eefb40d068a4124bcc66f384",
                       "edad76a75a604ccb0199e3e0a1c6ba8b0b1e9e4822fbca7857b33bd75a59751c",
                       "d5ab5fef0dc477db9327a29f745e1c1db1e5bb58e0e934b08788b2e3726f60e4",
                       "4562bbf761d761cafc89f9db9153eb74550eb23972ea4f9080e93681d9d90d1d",
                       "e3c8e9cf6defa8d5932c2f28b26362b12cd2101de7c67360166801f3fa933ada"):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                application.application(f"{GHCR}@sha256:{digest}")

    def test_registered_digest_is_accepted_from_either_registry(self):
        for repository in (GHCR, ECR):
            with self.subTest(repository=repository):
                image = f"{repository}@{PIN['index_digest']}"
                self.assertEqual(app_container(image=image)["image"], image)

    def test_unregistered_digest_is_refused_from_either_registry(self):
        unregistered = (LOCAL_BUILD, "sha256:" + "a" * 64, PIN["amd64_digest"])
        for repository in (GHCR, ECR):
            for digest in unregistered:
                with self.subTest(repository=repository, digest=digest[:15]), self.assertRaises(ValueError):
                    application.application(f"{repository}@{digest}")

    def test_cli_refuses_an_unregistered_digest_from_either_registry(self):
        script = str(WORKLOADS / "application.py")
        for repository in (GHCR, ECR):
            with self.subTest(repository=repository):
                result = subprocess.run([sys.executable, script, "--image", f"{repository}@{LOCAL_BUILD}"],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(LOCAL_BUILD, result.stderr)

    def test_general_render_path_has_no_switch_for_another_image(self):
        script = str(WORKLOADS / "application.py")
        usage = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(sorted(set(re.findall(r"--[a-z-]+", usage))), ["--help", "--image", "--print-image", "--pull-secret"])

    def test_pinned_image_has_the_expected_shape_and_is_accepted(self):
        self.assertRegex(PIN["source_sha"], r"\A[0-9a-f]{40}\Z")
        self.assertRegex(PIN["index_digest"], r"\Asha256:[0-9a-f]{64}\Z")
        self.assertRegex(PIN["amd64_digest"], r"\Asha256:[0-9a-f]{64}\Z")
        self.assertNotEqual(PIN["index_digest"], PIN["amd64_digest"])
        image = application.pinned_image()
        self.assertEqual(image, "ghcr.io/softbank-hackathon-finals/demo-chat@" + PIN["index_digest"])
        self.assertEqual(app_container(image=image)["image"], image)

    def test_the_image_is_written_in_only_one_place(self):
        # A new publication must change app-image.json alone. Evidence keeps what was actually measured.
        for relative in ("infra/envs/target-workloads/application.py", "infra/envs/target-workloads/README.md",
                         "scripts/export_target_image.py", "docs/operations/target-image-handoff.md",
                         "docs/adr/0017-target-app-secrets-manager-compat.md"):
            text = (ROOT / relative).read_text()
            for value in (PIN["source_sha"], PIN["index_digest"], PIN["amd64_digest"]):
                with self.subTest(file=relative, value=value[:15]):
                    self.assertNotIn(value, text)

    def test_cli_defaults_to_the_pinned_image_and_can_print_it(self):
        script = str(WORKLOADS / "application.py")
        printed = subprocess.run([sys.executable, script, "--print-image"], capture_output=True, text=True, check=True)
        self.assertEqual(printed.stdout.strip(), application.pinned_image())
        rendered_default = subprocess.run([sys.executable, script], capture_output=True, text=True, check=True)
        deployment = next(i for i in json.loads(rendered_default.stdout)["items"] if i["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["image"], application.pinned_image())

    def test_imported_image_must_not_fall_back_to_a_registry_pull(self):
        pod = pod_spec()
        self.assertNotIn("imagePullSecrets", pod)
        for container in [*pod["containers"], *pod["initContainers"]]:
            self.assertEqual(container["imagePullPolicy"], "Never")

    def test_registry_pull_uses_only_the_named_secret(self):
        pod = pod_spec(pull_secret="target-app-registry")
        self.assertEqual(pod["imagePullSecrets"], [{"name": "target-app-registry"}])
        for container in [*pod["containers"], *pod["initContainers"]]:
            self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        for bad in ("Not_A_Name", "", "a" * 254, "x\n"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                application.application(IMAGE, pull_secret=bad)

    def test_cli_prints_a_list_and_does_not_echo_rejected_input(self):
        ok = subprocess.run([sys.executable, str(WORKLOADS / "application.py"), "--image", IMAGE],
                            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(ok.stdout)["kind"], "List")
        bad = subprocess.run([sys.executable, str(WORKLOADS / "application.py"), "--image", "bad-canary"],
                             capture_output=True, text=True, check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, "")
        self.assertNotIn("bad-canary", bad.stderr)


class CompatServerTests(unittest.TestCase):
    SECRET_ID = "bravo/target/precutover/database"
    VALUE = '{"username": "chat_app", "password": "canary-value"}'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "credentials.json"
        self.path.write_text(self.VALUE)
        self.server = compat.make_server(self.SECRET_ID, self.path, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, body, target="secretsmanager.GetSecretValue", method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        connection.request(method, "/", payload, {"X-Amz-Target": target})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_answers_the_exact_secret(self):
        status, body = self.call({"SecretId": self.SECRET_ID})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body["SecretString"]), json.loads(self.VALUE))

    def test_refuses_other_secrets_and_operations_without_the_value(self):
        for request, target in (({"SecretId": "other"}, "secretsmanager.GetSecretValue"),
                                ({"SecretId": self.SECRET_ID}, "secretsmanager.PutSecretValue"),
                                ({"SecretId": self.SECRET_ID}, "secretsmanager.ListSecrets"),
                                (["not-an-object"], "secretsmanager.GetSecretValue"),
                                (b"not-json", "secretsmanager.GetSecretValue")):
            with self.subTest(request=request, target=target):
                status, body = self.call(request, target)
                self.assertEqual(status, 400)
                self.assertNotIn("canary-value", json.dumps(body))

    def test_only_the_path_the_sdk_uses_is_answered(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        connection.request("POST", "/any/other/path", json.dumps({"SecretId": self.SECRET_ID}).encode(),
                           {"X-Amz-Target": compat.OPERATION})
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertNotIn(b"canary-value", response.read())

    def test_refusal_tells_the_client_the_connection_is_closing(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        connection.request("POST", "/", b"not-json", {"X-Amz-Target": compat.OPERATION})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 400)
        self.assertTrue(response.will_close)

    def test_deeply_nested_request_is_refused_without_a_traceback(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            status, _ = self.call(b"[" * 60000)
        self.assertEqual(status, 400)
        self.assertEqual(errors.getvalue(), "")

    def test_requests_are_not_logged(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.call({"SecretId": self.SECRET_ID})
            self.call({"SecretId": "other"})
        self.assertEqual(errors.getvalue(), "")

    def test_rereads_the_file_so_rotation_reaches_the_app(self):
        self.path.write_text('{"username": "chat_app", "password": "rotated"}')
        _, body = self.call({"SecretId": self.SECRET_ID})
        self.assertEqual(json.loads(body["SecretString"])["password"], "rotated")

    def test_malformed_credentials_fail_without_echoing_the_file(self):
        for content in ('{"username": "chat_app"}', "canary-not-json", '{"username": "", "password": "x"}',
                        '{"username": "chat_app", "password": 1}', '["username", "password"]'):
            with self.subTest(content=content):
                self.path.write_text(content)
                status, body = self.call({"SecretId": self.SECRET_ID})
                self.assertEqual(status, 500)
                self.assertNotIn("canary", json.dumps(body))

    def test_missing_file_is_an_error_not_a_guess(self):
        self.path.unlink()
        status, _ = self.call({"SecretId": self.SECRET_ID})
        self.assertEqual(status, 500)

    def test_oversized_request_is_refused_even_when_otherwise_valid(self):
        padding = "x" * compat.MAX_REQUEST_BYTES
        status, body = self.call({"SecretId": self.SECRET_ID, "padding": padding})
        self.assertEqual(status, 400)
        self.assertNotIn("canary-value", json.dumps(body))

    def test_main_refuses_to_start_without_valid_credentials(self):
        script = WORKLOADS / "secrets_manager_compat.py"
        self.path.write_text("canary-not-json")
        environment = {**os.environ, "DB_SECRET_ID": self.SECRET_ID,
                       "BRAVO_COMPAT_CREDENTIALS": str(self.path), "BRAVO_COMPAT_PORT": "0"}
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                                env=environment, timeout=10, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "database credentials are unavailable or malformed")

    def test_sigterm_ends_the_server_at_once_even_as_process_one(self):
        # PID 1 has no default SIGTERM action; ignoring it would stall every rollout for the grace period.
        script = WORKLOADS / "secrets_manager_compat.py"
        environment = {**os.environ, "DB_SECRET_ID": self.SECRET_ID,
                       "BRAVO_COMPAT_CREDENTIALS": str(self.path), "BRAVO_COMPAT_PORT": "0"}
        process = subprocess.Popen([sys.executable, str(script)], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(process.communicate)
        self.addCleanup(process.kill)
        time.sleep(0.5)
        self.assertIsNone(process.poll())
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 0)

    @unittest.skipUnless(importlib.util.find_spec("boto3"), "boto3 is not installed")
    def test_the_real_sdk_call_the_app_makes_succeeds(self):
        import boto3

        with patch.dict(os.environ, {
                "AWS_ENDPOINT_URL_SECRETS_MANAGER": f"http://127.0.0.1:{self.server.server_address[1]}",
                "AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "local-signing-only",
                "AWS_SECRET_ACCESS_KEY": "local-signing-only"}):
            client = boto3.client("secretsmanager")
            secret = json.loads(client.get_secret_value(SecretId=self.SECRET_ID)["SecretString"])
        self.assertEqual((secret["username"], secret["password"]), ("chat_app", "canary-value"))


if __name__ == "__main__":
    unittest.main()
