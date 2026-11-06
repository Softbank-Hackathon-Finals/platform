"""Actual demo-chat HTTP + disposable PG, with a local credential-source substitute.

This never rotates team credentials. The immutable demo commit is mounted read-only;
only its Secrets Manager fetch function is replaced inside the isolated test process.
No published ports, cloud access, team database, or production PASS claim.
"""

import argparse
import io
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import psycopg
from psycopg import sql

from bravo_controls.runner import Environment, RunContext
from bravo_controls.secret_rotation import (
    SPEC,
    AppDatabaseCheck,
    ProcessIdentity,
    RotationTarget,
    RotationTrial,
    SecretRotationProbe,
    _hash,
)


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=90, check=False)
    if result.returncode:
        frames = re.findall(r'File "[^"\n]+", line ([0-9]+), in ([A-Za-z0-9_]+)', result.stderr)
        raise RuntimeError("local rotation fixture command failed; frames=" + json.dumps(frames))
    return result.stdout.strip()

def inside():
    os.umask(0o077)
    root = Path("/tmp/setup")
    environment = Environment("qualification", "source", "local:rotation")
    revision = Path("/demo/qualification-revision").read_text()
    target = RotationTarget("chat", environment.ref, "owned-local-app", "owned-local-postgres",
                            "chat_app", "local:credential-substitute", _hash(revision.encode()))
    admin = psycopg.connect(host="localhost", hostaddr="127.0.0.1", port=5432,
                            user="postgres", dbname="postgres",
                            password=(root / "admin-password").read_text(),
                            sslmode="verify-full", sslrootcert=str(root / "ca.crt"),
                            require_auth="scram-sha-256", channel_binding="require",
                            connect_timeout=3, autocommit=True)
    child = None
    summary = {}
    secret_file = root / "application-credential.json"

    def publish(password):
        temporary = secret_file.with_suffix(".new")
        temporary.write_text(json.dumps({"username": "chat_app", "password": password}))
        temporary.replace(secret_file)

    def change_password(password):
        admin.execute(sql.SQL("ALTER ROLE chat_app PASSWORD {}").format(sql.Literal(password)))

    def fresh_auth(password):
        try:
            with psycopg.connect(host="localhost", hostaddr="127.0.0.1", port=5432,
                                  user="chat_app", dbname="postgres", password=password,
                                  sslmode="verify-full", sslrootcert=str(root / "ca.crt"),
                                  require_auth="scram-sha-256", channel_binding="require",
                                  connect_timeout=2) as conn:
                return conn.execute("SELECT 1").fetchone() == (1,)
        except psycopg.OperationalError:
            # Fixture independently proves the same listener accepts the new credential.
            # The production adapter additionally needs server-side auth denial evidence.
            return False

    def start_app(frozen=False):
        nonlocal child
        started = datetime.now(UTC)
        command_text = """
import json, os, sys
sys.path.insert(0, '/demo')
from app import main
from app.config import Settings
import uvicorn
def fetch(_):
    with open('/tmp/setup/application-credential.json') as f:
        value = json.load(f)
    return value['username'], value['password']
initial = fetch(None)
main.fetch_db_credentials = (lambda _: initial) if os.environ['FROZEN'] == '1' else fetch
settings = Settings('local', 'localhost', 5432, 'postgres', 'local-substitute',
                    'unused', None, False, None, db_wait_seconds=0.25)
app = main.create_app(settings=settings, storage=object())
uvicorn.run(app, host='127.0.0.1', port=18080, log_level='critical', access_log=False)
"""
        env = {**os.environ, "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(root / "ca.crt"),
               "PGCHANNELBINDING": "require", "PGREQUIREAUTH": "scram-sha-256",
               "AWS_EC2_METADATA_DISABLED": "true", "FROZEN": "1" if frozen else "0"}
        child = subprocess.Popen([sys.executable, "-c", command_text], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process = ProcessIdentity("fixture-pod", "fixture-container", started, 0)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("demo application failed to start")
            try:
                if http("GET", "/api/health/db")[0] == 200:
                    return process
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError("demo application startup timeout")

    def stop_app():
        nonlocal child
        if child is not None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            child = None

    opener = build_opener(ProxyHandler({}))

    def http(method, path, body=None):
        req = Request("http://127.0.0.1:18080" + path, method=method,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            response = opener.open(req, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(100_000)
            return response.status, raw

    def check(process):
        start = datetime.now(UTC)
        marker = str(uuid4())
        body = "T14 credential rotation canary " + marker
        health, _ = http("GET", "/api/health/db")
        status, _ = http("POST", "/api/messages", {"idempotency_key": marker,
                                                   "author": "T14 qualification", "body": body})
        # Independent SQL readback, including the committing backend captured by a fixture trigger.
        rows = admin.execute("SELECT m.body, t.backend_started_at FROM public.messages m "
                             "JOIN public.rotation_trace t ON t.marker=m.idempotency_key "
                             "WHERE m.idempotency_key=%s", (marker,)).fetchall()
        end = datetime.now(UTC)
        digest = _hash(json.dumps({"marker": marker, "health": health, "write": status,
                                   "rows": len(rows), "body_equal": all(r[0] == body for r in rows)},
                                  sort_keys=True).encode())
        if any(row[0] != body for row in rows):
            raise RuntimeError("actual committed marker content mismatch")
        return AppDatabaseCheck(process, start, end, health, status, marker, len(rows),
                                 _hash(body.encode()), rows[0][1] if len(rows) == 1 else None, digest)

    try:
        admin.execute("CREATE ROLE chat_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                      "NOREPLICATION NOINHERIT")
        admin.execute(Path("/demo/db/schema.sql").read_text())
        admin.execute("CREATE TABLE public.rotation_trace (marker uuid PRIMARY KEY, "
                      "backend_started_at timestamptz NOT NULL)")
        admin.execute("""
            CREATE FUNCTION public.rotation_trace_insert() RETURNS trigger
            LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
            BEGIN
              INSERT INTO public.rotation_trace VALUES
                (NEW.idempotency_key, (SELECT backend_start FROM pg_stat_activity
                                       WHERE pid=pg_backend_pid()));
              RETURN NEW;
            END $$;
            REVOKE ALL ON FUNCTION public.rotation_trace_insert() FROM PUBLIC;
            CREATE TRIGGER rotation_trace AFTER INSERT ON public.messages
              FOR EACH ROW EXECUTE FUNCTION public.rotation_trace_insert();
        """)
        for frozen in (False, True):
            old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            change_password(old)
            publish(old)
            process = start_app(frozen)
            before = check(process)
            assert before.successful
            requested = datetime.now(UTC)
            monotonic_start = time.monotonic()
            change_password(new)
            publish(new)
            applied = datetime.now(UTC)
            old_rejected, new_accepted = not fresh_auth(old), fresh_auth(new)
            sessions = admin.execute("SELECT pid, backend_start FROM pg_stat_activity "
                                      "WHERE usename='chat_app' AND datname=current_database() "
                                      "AND application_name='demo-chat-local'").fetchall()
            assert sessions
            for pid, backend_start in sessions:
                closed = admin.execute("SELECT pg_terminate_backend(pid, 2000) FROM pg_stat_activity "
                                       "WHERE pid=%s AND backend_start=%s AND usename='chat_app' "
                                       "AND datname=current_database()", (pid, backend_start)).fetchall()
                assert closed == [(True,)]
            assert not admin.execute("SELECT 1 FROM pg_stat_activity WHERE usename='chat_app' "
                                      "AND backend_start <= %s", (applied,)).fetchall()
            fenced = datetime.now(UTC)
            while True:
                after = check(process)
                recovery_ms = int((time.monotonic() - monotonic_start) * 1000)
                if after.successful or recovery_ms >= 30_000:
                    break
                time.sleep(0.1)
            assert child.poll() is None
            context = RunContext("rotation-" + uuid4().hex, "chat", environment)
            trial = RotationTrial(context, target, "trial-" + uuid4().hex, before,
                                  requested, applied, fenced, "old-" + uuid4().hex,
                                  "new-" + uuid4().hex, old_rejected, new_accepted, True,
                                  after, recovery_ms, datetime.now(UTC),
                                  _hash((before.evidence_sha256 + after.evidence_sha256).encode()))
            retained = []
            current_process = [process]

            def current(*_, state=current_process, version=trial.new_version):
                if child.poll() is not None:
                    raise ValueError("application is not running")
                return target, state[0], version, datetime.now(UTC)

            probe = SecretRotationProbe(target, trial_provider=lambda _, value=trial: value,
                                         read_current=current, evidence_sink=retained.append)
            result = probe(context, SPEC)
            assert result.observed == ("failure" if frozen else "success")
            key = "credential_source_frozen" if frozen else "real_app_reauth"
            summary[key] = {"observed": result.observed, "recovery_ms": recovery_ms,
                            "old_sessions_closed": len(sessions), "app_restarted": False,
                            "new_marker_rows": after.committed_rows}
            if not frozen:
                stop_app()
                current_process[0] = start_app()
                assert probe(context, SPEC).observed == "failure"
                summary["actual_process_restart_rejected"] = True
            assert all(old not in raw.decode() and new not in raw.decode() for raw in retained)
            stop_app()
        summary["credential_source"] = "local-substitute-not-Secrets-Manager-or-OpenBao"
        summary["db_transport"] = "verify-full-SCRAM-channel-binding"
        summary["production_qualification"] = False
        return summary
    finally:
        stop_app()
        admin.close()


def main(demo_repo, revision):
    os.umask(0o077)

    def interrupted(*_):
        raise KeyboardInterrupt("local qualification interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    name = "bravo-t14-rotation-" + uuid4().hex[:10]
    image = command(
        "docker", "image", "inspect", "bravo-t14-rotation-qualification:20261106", "--format", "{{.Id}}"
    )
    cid = None
    summary = {}
    try:
        with tempfile.TemporaryDirectory(prefix="bravo-t14-rotation-") as folder:
            root = Path(folder)
            demo = root / "demo"
            demo.mkdir()
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("exact demo commit required")
            archive = subprocess.run(["git", "-C", str(demo_repo), "archive", revision],
                                     capture_output=True, timeout=30, check=True).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(demo, filter="data")
            (demo / "qualification-revision").write_text(revision)
            (root / "ext.cnf").write_text(
                "subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\n"
            )
            command(
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=Disposable rotation CA",
                "-keyout",
                str(root / "ca.key"),
                "-out",
                str(root / "ca.crt"),
            )
            command(
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=localhost",
                "-keyout",
                str(root / "tls.key"),
                "-out",
                str(root / "tls.csr"),
            )
            command(
                "openssl",
                "x509",
                "-req",
                "-in",
                str(root / "tls.csr"),
                "-CA",
                str(root / "ca.crt"),
                "-CAkey",
                str(root / "ca.key"),
                "-CAcreateserial",
                "-days",
                "1",
                "-extfile",
                str(root / "ext.cnf"),
                "-out",
                str(root / "tls.crt"),
            )
            mount = root / "fixture"
            mount.mkdir()
            for f in ("ca.crt", "tls.crt", "tls.key"):
                (mount / f).write_bytes((root / f).read_bytes())
            admin_password = secrets.token_urlsafe(32)
            (mount / "admin-password").write_text(admin_password)
            service_dir = Path(__file__).resolve().parents[2]
            cid = command(
                "docker",
                "create",
                "--name",
                name,
                "--label",
                "bravo.t14.fixture=" + name,
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--pids-limit",
                "128",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--env",
                "POSTGRES_PASSWORD_FILE=/tmp/setup/admin-password",
                "--env",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=trust",
                "--mount",
                f"type=bind,src={service_dir},dst=/work,readonly",
                "--mount", f"type=bind,src={demo},dst=/demo,readonly",
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                "exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/setup/tls.crt "
                "-c ssl_key_file=/tmp/setup/tls.key -c logging_collector=on -c log_destination=jsonlog "
                "-c log_directory=/tmp/logs -c log_filename=postgres -c log_timezone=UTC",
            )
            # Copy private fixtures before start, with the image's non-root identity.
            # No host chmod/chown, root exec, credential arguments or published ports.
            uid = int(
                command(
                    "docker", "run", "--rm", "--network", "none", "--entrypoint", "id", image, "-u"
                )
            )
            gid = int(
                command(
                    "docker", "run", "--rm", "--network", "none", "--entrypoint", "id", image, "-g"
                )
            )
            if uid == 0 or gid == 0:
                raise RuntimeError("qualification image must be non-root")
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for fixture in mount.iterdir():
                    data = fixture.read_bytes()
                    info = tarfile.TarInfo(fixture.name)
                    info.size, info.uid, info.gid, info.mode = len(data), uid, gid, 0o600
                    tar.addfile(info, io.BytesIO(data))
            copied = subprocess.run(
                ["docker", "cp", "-a", "-", cid + ":/tmp/setup"],
                input=archive.getvalue(),
                capture_output=True,
                timeout=10,
                check=False,
            )
            if copied.returncode:
                raise RuntimeError("private fixture copy failed")
            command("docker", "start", cid)
            deadline = time.monotonic() + 30
            while True:
                ready = subprocess.run(
                    ["docker", "exec", cid, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                    capture_output=True,
                    timeout=5,
                )
                if ready.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("fixture startup timeout")
                time.sleep(0.25)
            output = command(
                "docker",
                "exec",
                "-e",
                "PYTHONPATH=/work/src",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                cid,
                "/opt/qualification/bin/python",
                "/work/tests/integration/verify_secret_rotation.py",
                "--inside",
            )
            summary = json.loads(output)
            summary["image_id"] = image
            summary["demo_revision"] = revision
    finally:
        # Name is unique and ownership checked even if docker run lost its response.
        result = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)[0]
            if info["Config"].get("Labels", {}).get("bravo.t14.fixture") != name:
                raise RuntimeError("fixture ownership mismatch")
            command("docker", "rm", "-f", "-v", name)
        elif "No such" not in result.stderr:
            raise RuntimeError("fixture cleanup could not be confirmed")
        signal.signal(signal.SIGTERM, previous)
    summary["owned_container_removed"] = True
    print(json.dumps(summary))


if __name__ == "__main__":
    if sys.argv[1:] == ["--inside"]:
        print(json.dumps(inside(), sort_keys=True))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--demo-repo", type=Path, required=True)
        parser.add_argument("--demo-revision", required=True)
        args = parser.parse_args()
        main(args.demo_repo, args.demo_revision)
