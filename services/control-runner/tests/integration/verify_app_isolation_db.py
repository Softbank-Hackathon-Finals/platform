"""Disposable PG18.4 + psycopg qualification; no team/cloud resource changes.

Build the companion qualification Dockerfile first. DB operations run inside
that container so client endpoints are not translated by Docker Desktop NAT.
"""

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
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

from bravo_controls.aws_probes import _digest
from bravo_controls.postgres_isolation import AuditWindow, DatabasePair, PostgreSQLIsolationReader
from bravo_controls.runner import Environment, RunContext


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        frames = re.findall(r'File "[^"\n]+", line ([0-9]+), in ([A-Za-z0-9_]+)', result.stderr)
        last = result.stderr.splitlines()[-1].split(":", 1)[0] if result.stderr else "unknown"
        kind = last if re.fullmatch(r"[A-Za-z_.]+", last) else "unknown"
        raise RuntimeError(
            "local isolation fixture failed " + json.dumps({"kind": kind, "frames": frames})
        )
    return result.stdout.strip()


def inside():
    root = Path("/tmp/setup")
    cid, port, summary = "owned-local-postgres", 5432, {}
    admin = psycopg.connect(
        host="localhost",
        hostaddr="127.0.0.1",
        port=port,
        dbname="postgres",
        user="postgres",
        password=(root / "admin-password").read_text(),
        sslmode="verify-full",
        sslrootcert=str(root / "ca.crt"),
        connect_timeout=3,
        require_auth="scram-sha-256",
        channel_binding="require",
        autocommit=True,
    )
    try:
        passwords = {
            "chat_user": secrets.token_urlsafe(32),
            "other_user": secrets.token_urlsafe(32),
        }
        for user, database in (("chat_user", "chat_db"), ("other_user", "other_db")):
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD {}"
                ).format(sql.Identifier(user), sql.Literal(passwords[user]))
            )
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(user)
                )
            )
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database)
                )
            )
        dbs = dict(
            admin.execute(
                "SELECT datname, oid FROM pg_database WHERE datname IN ('chat_db','other_db')"
            ).fetchall()
        )
        roles = dict(
            admin.execute(
                "SELECT rolname, oid FROM pg_roles WHERE rolname IN ('chat_user','other_user')"
            ).fetchall()
        )
        environment = Environment("qualification", "source", "local/postgres")
        pair = DatabasePair(
            "chat",
            "other",
            environment,
            cid,
            "localhost",
            "127.0.0.1",
            port,
            root / "ca.crt",
            _digest((root / "ca.crt").read_bytes()),
            "chat_db",
            "other_db",
            "chat_user",
            "other_user",
            dbs["chat_db"],
            dbs["other_db"],
            roles["chat_user"],
            roles["other_user"],
        )

        def audit(ctx, target, application, start, end, endpoint):
            deadline = time.monotonic() + 3
            while True:
                raw = Path("/tmp/logs/postgres.json").read_text()
                lines = tuple(line.encode() for line in raw.splitlines())
                found = any(
                    str(json.loads(line).get("remote_port")) == str(endpoint[1]) for line in lines
                )
                if found or time.monotonic() >= deadline:
                    return AuditWindow(target.cluster_uid, start, end, True, lines, endpoint)
                time.sleep(0.1)

        reader = PostgreSQLIsolationReader(
            pair,
            passwords=passwords.__getitem__,
            read_registration=lambda _: pair.binding,
            read_audit=audit,
        )

        def attempt():
            return reader(RunContext("run-" + uuid4().hex, "chat", environment), str(uuid4()))

        result = attempt()
        if result.outcome != "deny":
            # Only structured nonsecret fields; never print server error text.
            raw = Path("/tmp/logs/postgres.json").read_text()
            diagnostic = [
                {k: row.get(k) for k in ("state_code", "error_severity", "application_name")}
                for row in map(json.loads, raw.splitlines())
                if row.get("error_severity") == "FATAL"
            ]
            raise RuntimeError("denial qualification failed: " + json.dumps(diagnostic))
        summary["denied_with_server_receipt"] = "PASS"
        admin.execute("GRANT CONNECT ON DATABASE chat_db TO other_user")
        assert attempt().outcome == "allow"
        summary["granted_cross_connect"] = "FAIL"
        admin.execute("REVOKE CONNECT ON DATABASE chat_db FROM other_user")
        reader.passwords = lambda user: (
            "wrong-fixture-password" if user == "other_user" else passwords[user]
        )
        assert attempt().reason == "POSITIVE_CONTROL_FAILED"
        summary["invalid_other_password"] = "UNKNOWN"
        reader.passwords = passwords.__getitem__
        reader.audit = lambda ctx, target, app, start, end, endpoint: AuditWindow(
            target.cluster_uid, start, end, True, (), endpoint
        )
        assert attempt().outcome is None
        summary["missing_server_receipt"] = "UNKNOWN"
        reader.audit = audit
        reader.pair = replace(pair, other_role_oid=pair.other_role_oid + 10000)
        reader.registration = lambda _: reader.binding
        assert attempt().reason == "POSITIVE_CONTROL_FAILED"
        summary["changed_role_oid"] = "UNKNOWN"
        summary["operational_qualification"] = False
        print(json.dumps(summary))
    finally:
        admin.close()


def main():
    os.umask(0o077)

    def interrupted(*_):
        raise KeyboardInterrupt("local qualification interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    name = "bravo-t14-iso-" + uuid4().hex[:10]
    image = command(
        "docker", "image", "inspect", "bravo-t14-iso-qualification:20261106", "--format", "{{.Id}}"
    )
    cid = None
    summary = {}
    try:
        with tempfile.TemporaryDirectory(prefix="bravo-t14-iso-") as folder:
            root = Path(folder)
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
                "/CN=Disposable isolation CA",
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
                "384m",
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
                "/work/tests/integration/verify_app_isolation_db.py",
                "--inside",
            )
            summary = json.loads(output)
            summary["image_id"] = image
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
    inside() if sys.argv[1:] == ["--inside"] else main()
