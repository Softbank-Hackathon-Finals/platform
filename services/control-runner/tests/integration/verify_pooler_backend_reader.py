"""Actual PostgreSQL/PgBouncer readback in two disposable non-root containers.

No AWS, Kubernetes or external network. No published ports. Both containers
share one `--network none` namespace and fixed fixtures, not user code.
Build the existing testing/secret-rotation.Dockerfile to supply the Python/PG
qualification image; this test does not start the demo app or rotate its secrets.
"""

import argparse
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def command(*args, input=None, timeout=90):
    result = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("owned pooler fixture command failed")
    return result.stdout.decode().strip()


def inside(mode):
    import psycopg
    from psycopg import sql

    from bravo_controls.pooler_backend_tls import (
        SPEC,
        BackendTarget,
        PoolerBackendTLSProbe,
        PostgreSQLPoolerBackendReader,
    )
    from bravo_controls.runner import Environment, RunContext

    root = Path("/tmp/setup")
    common = dict(
        host="localhost",
        hostaddr="127.0.0.1",
        sslmode="verify-full",
        sslrootcert=str(root / "ca.crt"),
        require_auth="scram-sha-256",
        connect_timeout=5,
        autocommit=True,
    )
    if mode == "setup":
        with psycopg.connect(
            **common,
            port=5432,
            dbname="postgres",
            user="postgres",
            password=(root / "admin-password").read_text(),
        ) as admin:
            admin.execute(
                sql.SQL(
                    "CREATE ROLE chat_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Literal((root / "app-password").read_text()))
            )
            admin.execute("CREATE DATABASE chat OWNER chat_app")
        return {"setup": True}

    environment = Environment("owned-local-source", "source", "local:qualification")
    target = BackendTarget(
        "fixture-chat",
        environment,
        "owned-pooler",
        "owned-pooler-container",
        "owned-postgres-container",
        "chat",
        "127.0.0.1",
        "127.0.0.1",
        "sha256:" + "a" * 64,
    )
    # This fixed fixture registration is not an operational cluster registration.
    with psycopg.connect(
        **common,
        port=6432,
        dbname="pgbouncer",
        user="stats_reader",
        password=(root / "stats-password").read_text(),
    ) as stats:

        def connect():
            return psycopg.connect(
                **common,
                port=6432,
                dbname="chat",
                user="chat_app",
                password=(root / "app-password").read_text(),
            )

        reader = PostgreSQLPoolerBackendReader(
            stats,
            open_application_connection=connect,
            application_user="chat_app",
            stats_user="stats_reader",
            pooler_hostname="localhost",
            pooler_port=6432,
            read_registration=lambda *_: target.binding(),
        )
        summaries = []
        for _trial in range(2 if mode == "verify-full" else 1):
            evidence = []
            ctx = RunContext("local-" + uuid4().hex, target.app, environment)
            observation = PoolerBackendTLSProbe(
                target, collector=reader, evidence_sink=evidence.append
            )(ctx, SPEC)
            summaries.append(
                {
                    "observed": observation.observed,
                    "reason": observation.unknown_reason,
                    "facts": json.loads(evidence[0])["facts"],
                }
            )
        if mode == "verify-full":
            assert summaries[0]["observed"] == "enabled"
            assert (
                summaries[1]["observed"] is None and summaries[1]["reason"] == "UNEXPECTED_RESPONSE"
            )
        elif mode in {"verify-ca", "disable"}:
            assert summaries[0]["observed"] == "disabled"
        else:
            assert summaries[0]["observed"] is None
        return {"mode": mode, "trials": summaries}


def main(postgres_image, pooler_image):
    os.umask(0o077)
    name = "bravo-t14-backend-" + uuid4().hex[:10]
    created = []
    summary = {"started_at": datetime.now(UTC).isoformat(), "cases": []}

    def interrupted(*_):
        raise KeyboardInterrupt("owned pooler qualification interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)

    def remove(container_name):
        result = subprocess.run(
            ["docker", "inspect", container_name], capture_output=True, timeout=10
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)[0]
            if info["Config"].get("Labels", {}).get("bravo.t14.fixture") != name:
                raise RuntimeError("fixture ownership mismatch")
            command("docker", "rm", "-f", "-v", container_name)
        elif f"no such object: {container_name}".encode() not in result.stderr.lower():
            raise RuntimeError("fixture cleanup unconfirmed")

    def identity(image):
        uid = int(
            command("docker", "run", "--rm", "--network", "none", "--entrypoint", "id", image, "-u")
        )
        gid = int(
            command("docker", "run", "--rm", "--network", "none", "--entrypoint", "id", image, "-g")
        )
        if not uid or not gid:
            raise RuntimeError("non-root qualification image required")
        return uid, gid

    def copy_files(cid, files, uid, gid, destination):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for filename, data in files.items():
                entry = tarfile.TarInfo(filename)
                entry.size, entry.uid, entry.gid, entry.mode = len(data), uid, gid, 0o600
                tar.addfile(entry, io.BytesIO(data))
        command("docker", "cp", "-a", "-", cid + ":" + destination, input=archive.getvalue())

    def exec_probe(cid, mode):
        return json.loads(
            command(
                "docker",
                "exec",
                "-e",
                "PYTHONPATH=/work/src",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                cid,
                "/opt/qualification/bin/python",
                "/work/tests/integration/verify_pooler_backend_reader.py",
                "--inside",
                mode,
                timeout=45,
            )
        )

    try:
        pg_image = command("docker", "image", "inspect", postgres_image, "--format", "{{.Id}}")
        pool_image = command("docker", "image", "inspect", pooler_image, "--format", "{{.Id}}")
        pg_identity, pool_identity = identity(pg_image), identity(pool_image)
        summary.update(postgres_image=pg_image, pooler_image=pool_image)
        with tempfile.TemporaryDirectory(prefix="bravo-t14-backend-") as folder:
            root = Path(folder)
            (root / "ext.cnf").write_text(
                "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
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
                "/CN=Owned backend CA",
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
                "/CN=Wrong backend CA",
                "-keyout",
                str(root / "wrong.key"),
                "-out",
                str(root / "wrong.crt"),
            )
            # CA private keys stay outside both containers.
            tls = {
                f: (root / f).read_bytes() for f in ("ca.crt", "tls.crt", "tls.key", "wrong.crt")
            }
            passwords = {
                f: secrets.token_urlsafe(32).encode()
                for f in ("admin-password", "app-password", "stats-password")
            }
            service = Path(__file__).resolve().parents[2]
            pg_name = name + "-pg"
            created.append(pg_name)
            cid = command(
                "docker",
                "create",
                "--name",
                pg_name,
                "--label",
                "bravo.t14.fixture=" + name,
                "--network",
                "none",
                "--cpus",
                "1",
                "--memory",
                "384m",
                "--pids-limit",
                "128",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--env",
                "POSTGRES_PASSWORD_FILE=/tmp/setup/admin-password",
                "--env",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=trust",
                "--mount",
                f"type=bind,src={service},dst=/work,readonly",
                pg_image,
                "postgres",
                "-c",
                "ssl=on",
                "-c",
                "ssl_cert_file=/tmp/setup/tls.crt",
                "-c",
                "ssl_key_file=/tmp/setup/tls.key",
            )
            copy_files(cid, {**tls, **passwords}, *pg_identity, "/tmp/setup")
            command("docker", "start", cid)
            end = time.monotonic() + 30
            while subprocess.run(
                ["docker", "exec", cid, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                capture_output=True,
                timeout=5,
            ).returncode:
                if time.monotonic() > end:
                    raise RuntimeError("owned PostgreSQL readiness timeout")
                time.sleep(0.2)
            exec_probe(cid, "setup")
            for mode in ("verify-full", "verify-ca", "disable", "wrong-ca"):
                pool_name = name + "-pool-" + mode
                created.append(pool_name)
                config = f"""[databases]
chat = host=127.0.0.1 port=5432 dbname=chat
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = 6432
unix_socket_dir =
auth_type = scram-sha-256
auth_file = /tmp/userlist.txt
stats_users = stats_reader
pool_mode = transaction
max_client_conn = 10
default_pool_size = 2
server_connect_timeout = 3
query_wait_timeout = 3
client_tls_sslmode = require
client_tls_cert_file = /tmp/tls.crt
client_tls_key_file = /tmp/tls.key
server_tls_sslmode = {"verify-full" if mode == "wrong-ca" else mode}
server_tls_ca_file = /tmp/{"wrong.crt" if mode == "wrong-ca" else "ca.crt"}
log_connections = 0
log_disconnections = 0
"""
                users = f'"chat_app" "{passwords["app-password"].decode()}"\n"stats_reader" "{passwords["stats-password"].decode()}"\n'
                pool = command(
                    "docker",
                    "create",
                    "--name",
                    pool_name,
                    "--label",
                    "bravo.t14.fixture=" + name,
                    "--network",
                    "container:" + cid,
                    "--cpus",
                    "0.5",
                    "--memory",
                    "128m",
                    "--pids-limit",
                    "64",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--entrypoint",
                    "/usr/sbin/pgbouncer",
                    pool_image,
                    "/tmp/t14.ini",
                )
                copy_files(
                    pool,
                    {**tls, "t14.ini": config.encode(), "userlist.txt": users.encode()},
                    *pool_identity,
                    "/tmp",
                )
                command("docker", "start", pool)
                end = time.monotonic() + 10
                while subprocess.run(
                    ["docker", "exec", cid, "pg_isready", "-h", "127.0.0.1", "-p", "6432"],
                    capture_output=True,
                    timeout=3,
                ).returncode:
                    if time.monotonic() > end:
                        raise RuntimeError("owned PgBouncer readiness timeout")
                    time.sleep(0.2)
                summary["cases"].append(exec_probe(cid, mode))
                remove(pool_name)
    finally:
        cleanup_errors = []
        for container_name in reversed(created):
            try:
                remove(container_name)
            except Exception:
                cleanup_errors.append(container_name)
        signal.signal(signal.SIGTERM, previous)
        if cleanup_errors:
            raise RuntimeError("owned fixture cleanup requires inspection")
    summary.update(owned_containers_removed=True, finished_at=datetime.now(UTC).isoformat())
    print(json.dumps(summary))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--inside"]:
        print(json.dumps(inside(sys.argv[2])))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--postgres-image", default="bravo-t14-rotation-qualification:20261106")
        parser.add_argument(
            "--pooler-image",
            default="ghcr.io/cloudnative-pg/pgbouncer@sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8",
        )
        args = parser.parse_args()
        main(args.postgres_image, args.pooler_image)
