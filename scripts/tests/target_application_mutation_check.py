"""Break the T30 target application one line at a time; the unit tests must notice.

Runs on temporary copies only. Prints one JSON summary and exits non-zero if any
mutation survives or no longer applies.

    python3 scripts/tests/target_application_mutation_check.py
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
A = "infra/envs/target-workloads/application.py"
C = "infra/envs/target-workloads/secrets_manager_compat.py"
# Only a test method counts. An import error measures nothing.
RESULT_LINE = re.compile(r"^(?:FAIL|ERROR): test_\w+ \(test_target_application\.", re.MULTILINE)
COPIED = ("infra/envs/target-workloads", "infra/envs/target-secrets", "scripts/tests/test_target_application.py")
MUTATIONS = [
 (A, 'APP_ENV = "onprem"', 'APP_ENV = "aws"'),
 (A, '"WRITE_BLOCKED": "true"', '"WRITE_BLOCKED": "false"'),
 (A, 'f"target-pgbouncer.{NAMESPACE}.svc.cluster.local"', 'f"target-db-rw.{NAMESPACE}.svc.cluster.local"'),
 (A, '"PGSSLMODE": "verify-full"', '"PGSSLMODE": "require"'),
 (A, '"ATTACHMENT_UPLOAD_ENABLED": "false"', '"ATTACHMENT_UPLOAD_ENABLED": "true"'),
 (A, '"sha256:4562bbf761d761cafc89f9db9153eb74550eb23972ea4f9080e93681d9d90d1d"}', '"sha256:0000"}'),
 (A, 'f"http://127.0.0.1:{COMPAT_PORT}"', '"https://secretsmanager.ap-northeast-2.amazonaws.com"'),
 (A, '"AWS_EC2_METADATA_DISABLED": "true"', '"AWS_EC2_METADATA_DISABLED": "false"'),
 (A, 'secret_env("ADMIN_TOKEN", "target-app-admin", "token")', 'secret_env("ADMIN_TOKEN", "target-app-db", "password")'),
 (A, '"target-app-objects", "access_key"', '"target-object-client", "AWS_ACCESS_KEY_ID"'),
 (A, '"secretName": "target-app-credentials"', '"secretName": "target-app-db"'),
 (A, '"key": "credentials.json", "path": "credentials.json"', '"key": "password", "path": "credentials.json"'),
 (A, '"restartPolicy": "Always",', ''),
 (A, '"bravo-object-client": "true"', '"bravo-object-client": "false"'),
 (A, '"annotations": {"bravo.t30/not-exit-evidence": "secrets-manager-compat-sidecar"},', ''),
 (A, '"type": "ClusterIP", "selector": selector', '"type": "NodePort", "selector": selector'),
 (A, '"name": "app", "image": image', '"name": "demo", "image": image'),
 (A, '"lifecycle": {"preStop": {"sleep": {"seconds": 5}}},', ''),
 (A, '"automountServiceAccountToken": False,', '"automountServiceAccountToken": True,'),
 (A, '"readOnlyRootFilesystem": True', '"readOnlyRootFilesystem": False'),
 (A, '"runAsNonRoot": True, "runAsUser": 10001', '"runAsNonRoot": False, "runAsUser": 10001'),
 (A, 'pull_policy = "IfNotPresent" if pull_secret else "Never"', 'pull_policy = "IfNotPresent"'),
 (A, 'if image.split("@")[1] in WITHOUT_ENV_HEADER:', 'if False:'),
 (A, 'not IMAGE.fullmatch(image)', 'not IMAGE.match(image)'),
 (A, 'if pull_secret:\n        pod["imagePullSecrets"]', 'if False:\n        pod["imagePullSecrets"]'),
 (A, '"app": NAME}', '"app": "other"}'),
 (A, 'parser.error(str(error))', 'parser.error(str(error) + " " + args.image)'),
 (A, '"name": "database-credentials", "mountPath": str(Path(CREDENTIALS_FILE).parent), "readOnly": True', '"name": "database-credentials", "mountPath": "/secrets", "readOnly": False'),
 (C, 'ThreadingHTTPServer(("127.0.0.1", port), Handler)', 'ThreadingHTTPServer(("0.0.0.0", port), Handler)'),
 (C, 'or self.headers.get("X-Amz-Target") != OPERATION', ''),
 (C, 'or request.get("SecretId") != secret_id', ''),
 (C, 'not isinstance(request, dict) or', ''),
 (C, 'if not 0 < length <= MAX_REQUEST_BYTES', 'if False'),
 (C, 'isinstance(value.get(key), str) and value[key]', 'key in value'),
 (C, 'not isinstance(value, dict) or not all(', 'not all('),
 (C, 'text = read_credentials(credentials_path)', 'text = cached'),
 (C, 'except (OSError, ValueError):\n                return self.reply(500', 'except (ValueError):\n                return self.reply(500'),
 (C, '        read_credentials(path)\n    except', '        pass\n    except'),
 (C, 'sys.exit("database credentials are unavailable or malformed")', 'raise'),
 (C, 'signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))', 'pass'),
 (A, '"startupProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 5, "failureThreshold": 60},', ''),
 (A, '"timeoutSeconds": 5, "periodSeconds": 2, "failureThreshold": 60', '"timeoutSeconds": 1, "periodSeconds": 1, "failureThreshold": 30'),
 (A, '["/bin/bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{COMPAT_PORT}"]', '[PYTHON, "-c", "pass"]'),
]


def survives(path, old, new):
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder)
        for relative in COPIED:
            source, destination = ROOT / relative, copy / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "image"))
            else:
                shutil.copy(source, destination)
        target = copy / path
        text = target.read_text()
        if old not in text:
            raise ValueError(f"mutation no longer applies: {old[:60]}")
        target.write_text(text.replace(old, new, 1))
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(copy / "scripts/tests"),
             "-p", "test_target_application.py"],
            capture_output=True, text=True, cwd=copy, timeout=120, check=False)
        return not RESULT_LINE.search(result.stderr)


def main():
    survived = [old[:70] for path, old, new in MUTATIONS if survives(path, old, new)]
    print(json.dumps({"mutations": len(MUTATIONS), "killed": len(MUTATIONS) - len(survived), "survived": survived}))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
