"""Fixed demo-chat canary qualification in an owned, offline PostgreSQL fixture.

No shared workload, AWS credential, CloudWatch or production claim. The existing
fixture launcher pins a Git revision, mounts it read-only and runs as non-root.
The canary is a credential only for this disposable application's admin route.
"""

import argparse
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import psycopg
from psycopg import sql

from bravo_controls.audit_probes import AuditEvent
from bravo_controls.runner import Environment, RunContext
from bravo_controls.secret_logs import SPEC, LogCanaryReceipt, LogWindow, SecretLogProbe

CHILD = """
import json, logging, os, sys
sys.path.insert(0, '/demo')
from app import main
from app.config import Settings
import uvicorn
with open('/tmp/setup/log-canary-config.json') as f:
    config = json.load(f)
with open('/tmp/setup/log-canary-db.json') as f:
    db = json.load(f)
main.fetch_db_credentials = lambda _: (db['username'], db['password'])
os.environ['ADMIN_TOKEN'] = config['admin_token']
settings = Settings.from_env()
assert settings.admin_token == config['admin_token']
if config.get('marker'):
    if config['mode'] != 'missing_stdout':
        print(config['marker'], flush=True)
    print(config['marker'], file=sys.stderr, flush=True)
if config['mode'] == 'deliberate_leak':
    print(config['admin_token'], file=sys.stderr, flush=True)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
app = main.create_app(settings=settings, storage=object())
uvicorn.run(app, host='127.0.0.1', port=18080, log_level='info', access_log=True)
"""


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def inside():
    os.umask(0o077)
    root = Path("/tmp/setup")
    config_file = root / "log-canary-config.json"
    original_token = secrets.token_urlsafe(40)
    original = json.dumps({"admin_token": original_token, "mode": "baseline"}).encode()
    config_file.write_bytes(original)
    credential = secrets.token_urlsafe(40)
    (root / "log-canary-db.json").write_text(
        json.dumps({"username": "chat_app", "password": credential})
    )
    admin = psycopg.connect(
        host="localhost",
        hostaddr="127.0.0.1",
        port=5432,
        dbname="postgres",
        user="postgres",
        password=(root / "admin-password").read_text(),
        sslmode="verify-full",
        sslrootcert=str(root / "ca.crt"),
        require_auth="scram-sha-256",
        channel_binding="require",
        connect_timeout=3,
        autocommit=True,
    )
    admin.execute(
        sql.SQL(
            "CREATE ROLE chat_app LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB PASSWORD {}"
        ).format(sql.Literal(credential))
    )
    # Same application migration as the prior local DB qualification, fixed source.
    admin.execute(Path("/demo/db/schema.sql").read_text())
    admin.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO chat_app")
    admin.close()
    opener = build_opener(ProxyHandler({}))
    processes, threads, events, errors = [], [], [], []
    shutdown_confirmed = {}
    lock = threading.Lock()
    byte_count = [0]

    def now():
        return datetime.now(UTC)

    def http(method, path, *, token=None, body=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        request = Request(
            "http://127.0.0.1:18080" + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            response = opener.open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            response.read(65536)
            return response.status

    def capture(pipe, stream, pid):
        try:
            while raw := pipe.readline(65537):
                timestamp = now()
                with lock:
                    byte_count[0] += len(raw)
                    if byte_count[0] > 2 * 1024 * 1024 or len(raw) > 65536:
                        errors.append("output_limit")
                        continue
                    text = raw.decode("utf-8", errors="strict")
                    if text.strip() == "INFO:     Application shutdown complete.":
                        shutdown_confirmed[pid] = True
                    events.append(AuditEvent(str(len(events)), stream, timestamp, timestamp, text))
        except Exception:
            errors.append("capture_failed")
        finally:
            pipe.close()

    def start():
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "PGSSLMODE": "verify-full",
            "PGSSLROOTCERT": str(root / "ca.crt"),
            "PGCHANNELBINDING": "require",
            "PGREQUIREAUTH": "scram-sha-256",
            "AWS_EC2_METADATA_DISABLED": "true",
            "APP_ENV": "local",
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "DB_NAME": "postgres",
            "DB_SECRET_ID": "owned-local-substitute",
            "S3_BUCKET": "unused",
        }
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        processes.append(child)
        shutdown_confirmed[child.pid] = False
        for stream, pipe in (("stdout", child.stdout), ("stderr", child.stderr)):
            thread = threading.Thread(target=capture, args=(pipe, stream, child.pid), daemon=True)
            thread.start()
            threads.append(thread)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child.poll() is not None or errors:
                raise RuntimeError("owned demo startup/capture failed")
            try:
                if http("GET", "/api/health/db") == 200:
                    return child
            except OSError:
                pass
            time.sleep(0.05)
        raise RuntimeError("owned demo startup deadline")

    def stop(child):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
                errors.append("forced_termination")
        if child.returncode not in (0, -signal.SIGTERM):
            errors.append("unclean_exit")

    summary = {
        "cases": [],
        "log_source": "owned-process-pipes-not-CloudWatch",
        "production_qualification": False,
    }
    try:
        baseline = start()
        assert http("GET", "/admin/write-block", token=original_token) == 200
        stop(baseline)
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
        threads.clear()
        assert not errors and shutdown_confirmed[baseline.pid]
        for mode in ("clean", "deliberate_leak", "missing_stdout"):
            events.clear()
            byte_count[0] = 0
            secret, marker = (
                "bravo_secret_" + secrets.token_hex(32),
                "bravo_log_" + secrets.token_hex(16),
            )
            injected = json.dumps({"admin_token": secret, "marker": marker, "mode": mode}).encode()
            assert config_file.read_bytes() == original
            started = now()
            config_file.write_bytes(injected)
            try:
                child = start()
                assert http("GET", "/admin/write-block", token=original_token) == 401
                assert http("GET", "/admin/write-block", token=secret) == 200
                assert (
                    http("PUT", "/admin/write-block", token=secret, body={"blocked": True}) == 200
                )
                assert (
                    http("PUT", "/admin/write-block", token=secret, body={"blocked": False}) == 200
                )
                assert (
                    http(
                        "POST",
                        "/api/messages",
                        body={
                            "idempotency_key": str(uuid4()),
                            "author": "T14 canary",
                            "body": "owned log trial",
                        },
                    )
                    == 201
                )
                stop(child)
            finally:
                # Restore only our unchanged injected configuration. Never overwrite drift.
                if config_file.read_bytes() != injected:
                    raise RuntimeError("owned configuration changed; restore refused")
                config_file.write_bytes(original)
            restored = start()
            assert http("GET", "/admin/write-block", token=original_token) == 200
            assert http("GET", "/admin/write-block", token=secret) == 401
            assert http("GET", "/api/health/db") == 200
            stop(restored)
            for thread in threads:
                thread.join(3)
                assert not thread.is_alive()
            threads.clear()
            assert not errors and config_file.read_bytes() == original
            assert shutdown_confirmed[child.pid] and shutdown_confirmed[restored.pid]
            ended, settled = now(), now()
            ctx = RunContext(
                "canary-" + uuid4().hex,
                "demo-chat",
                Environment("local", "source", "local:owned-log-trial"),
            )
            configuration = digest(original + Path("/demo/qualification-revision").read_bytes())
            receipt = LogCanaryReceipt(
                ctx,
                "owned-pipes",
                ("stdout", "stderr"),
                configuration,
                secret,
                marker,
                started,
                ended,
                settled,
                frozenset({"configured", "exercised", "restored", "logs_drained"}),
            )
            retained = []

            def search(_, fixed=receipt, captured=tuple(events)):
                return LogWindow(
                    fixed.context,
                    fixed.source_binding,
                    fixed.streams,
                    fixed.started_at,
                    fixed.ended_at,
                    now(),
                    captured,
                    True,
                    True,
                )

            observation = SecretLogProbe(
                receipt_provider=lambda *_, fixed=receipt: fixed,
                search=search,
                source_binding=receipt.source_binding,
                streams=receipt.streams,
                configuration_sha256=configuration,
                evidence_sink=retained.append,
            )(ctx, SPEC)
            expected = {"clean": "absent", "deliberate_leak": "present", "missing_stdout": None}[
                mode
            ]
            assert observation.observed == expected
            assert all(
                value not in retained[0].decode()
                for value in (secret, marker, original_token, credential)
            )
            facts = json.loads(retained[0])
            summary["cases"].append(
                {
                    "case": mode,
                    "observed": observation.observed,
                    "reason": observation.unknown_reason,
                    "events": facts["searched_events"],
                    "leaking_events": facts["leaking_events"],
                    "positive_streams": facts["positive_streams"],
                    "configuration_restored": True,
                    "original_auth_restored": True,
                    "all_processes_exited_and_pipes_eof": True,
                    "evidence_sha256": observation.output_sha256,
                    "retained_evidence": facts,
                }
            )
        return summary
    finally:
        for child in processes:
            if child.poll() is None:
                stop(child)
        for thread in threads:
            thread.join(3)
        config_file.unlink(missing_ok=True)
        (root / "log-canary-db.json").unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--inside"]:
        print(json.dumps(inside()))
    else:
        from verify_secret_rotation import main

        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--demo-repo", type=Path, required=True)
        parser.add_argument("--demo-revision", required=True)
        args = parser.parse_args()
        main(args.demo_repo, args.demo_revision, qualification_script="verify_demo_log_canary.py")
