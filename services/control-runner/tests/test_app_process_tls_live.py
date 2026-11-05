"""Opt-in real PG18/PgBouncer TLS; no team resources or application data.

BRAVO_APP_TLS_DOCKER=1 uv run pytest tests/test_app_process_tls_live.py
The host registration/readback is a local fixture, not Kubernetes attestation.
"""

import json
import os
import secrets
import select
import socket
import socketserver
import struct
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Thread

import psycopg
import pytest
from psycopg import sql

from bravo_controls import Catalog, Environment, Runner
from bravo_controls.app_process_tls import (
    CLIENT,
    AppProcessRegistration,
    AppProcessTLSProbe,
    digest,
    encoded,
)
from bravo_controls.postgres_tls import PoolerTarget

PG = "public.ecr.aws/docker/library/postgres:18.4@sha256:a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636"
POOLER = "ghcr.io/cloudnative-pg/pgbouncer:1.25.1@sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8"
pytestmark = pytest.mark.skipif(os.environ.get("BRAVO_APP_TLS_DOCKER") != "1", reason="opt-in Docker TLS qualification")


def command(*args, **kwargs):
    completed = subprocess.run(args, capture_output=True, text=True, timeout=60, **kwargs)
    if completed.returncode:
        # Do not print argv/env or server logs (they can contain local fixture credentials).
        raise RuntimeError("local TLS fixture command failed")
    return completed.stdout.strip()


def test_real_tls_controls_and_missing_retention_never_pass(tmp_path):
    command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=local-pooler", "-addext", "subjectAltName=DNS:pooler.test,DNS:local-db",
            "-keyout", str(tmp_path / "server.key"), "-out", str(tmp_path / "server.crt"))
    (tmp_path / "server.key").chmod(0o600)
    (tmp_path / "server.crt").chmod(0o644)
    password = secrets.token_urlsafe(32)
    (tmp_path / "userlist.txt").write_text('"chat_app" "' + password + '"\n')
    (tmp_path / "userlist.txt").chmod(0o600)
    (tmp_path / "pgbouncer.ini").write_text("""[databases]
chat = host=local-db port=5432 dbname=chat
[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
unix_socket_dir = /tmp
pidfile = /tmp/pgbouncer.pid
auth_type = scram-sha-256
auth_file = /tmp/runtime/userlist.txt
pool_mode = transaction
server_tls_sslmode = verify-full
server_tls_ca_file = /tmp/runtime/server.crt
client_tls_sslmode = require
client_tls_cert_file = /tmp/runtime/server.crt
client_tls_key_file = /tmp/runtime/server.key
ignore_startup_parameters = extra_float_digits,options
""")
    network = command("docker", "network", "create", "t14-app-tls-" + secrets.token_hex(6))
    containers = []
    try:
        db = command("docker", "run", "-d", "--network", network, "--network-alias", "local-db",
                     "--memory", "384m", "--cpus", "1", "--tmpfs", "/var/lib/postgresql:rw,size=268435456",
                     "-e", "POSTGRES_PASSWORD", "--mount", f"type=bind,src={tmp_path},dst=/fixture,readonly",
                     "--entrypoint", "sh", PG, "-c",
                     "cp /fixture/server.key /tmp/server.key && chown postgres /tmp/server.key && chmod 600 /tmp/server.key && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/fixture/server.crt -c ssl_key_file=/tmp/server.key",
                     env=dict(os.environ, POSTGRES_PASSWORD=password))
        containers.append(db)
        for attempt in range(60):
            try:
                command("docker", "exec", db, "pg_isready", "-U", "postgres")
                break
            except RuntimeError:
                if attempt == 59:
                    raise
                time.sleep(.5)
        setup = ("CREATE DATABASE chat;\n" + sql.SQL("CREATE ROLE chat_app LOGIN PASSWORD {};\n").format(
            sql.Literal(password)).as_string() + "ALTER ROLE chat_app SET default_transaction_read_only=on;\n")
        command("docker", "exec", "-i", db, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", input=setup)
        pooler = command("docker", "run", "-d", "--network", network, "-p", "127.0.0.1::5432",
                         "--user", "0:0", "--read-only", "--tmpfs", "/tmp:rw,size=33554432",
                         "--memory", "96m", "--cpus", ".5",
                         "--mount", f"type=bind,src={tmp_path},dst=/fixture,readonly",
                         "--entrypoint", "/bin/sh", POOLER, "-c",
                         "cp -R /fixture /tmp/runtime && chown -R 998:996 /tmp/runtime && chmod 600 /tmp/runtime/server.key && exec setpriv --reuid=998 --regid=996 --clear-groups /usr/sbin/pgbouncer /tmp/runtime/pgbouncer.ini")
        containers.append(pooler)
        port = int(command("docker", "port", pooler, "5432").rsplit(":", 1)[1])
        environment = Environment("local", "rehearsal", "local:docker/app-tls")
        target = PoolerTarget("demo-chat", environment, "local", "local-app-process", "local-pooler",
                              "sha256:" + "a" * 64, "pooler.test", "127.0.0.1", port, "chat", "chat_app",
                              (tmp_path / "server.crt").read_bytes(),
                              (Path(__file__).parent / "fixtures/tls/wrong-ca.pem").read_bytes())
        executable = Path(sys.executable)
        reg = AppProcessRegistration(target, "local-deployment", "sha256:" + "b" * 64,
                                     "sha256:" + "c" * 64, "sha256:" + "d" * 64,
                                     executable, digest(executable.read_bytes()), digest(CLIENT.read_bytes()),
                                     psycopg.__version__, psycopg.pq.version())
        controls = Path(__file__).resolve().parents[3] / "docs/controls"
        catalog = Catalog(json.loads((controls / "profiles.json").read_text()),
                          json.loads((controls / "control-result.schema.json").read_text()))
        for rejected in (False, True):
            proofs = []

            def sink(payload, reject=rejected, retained=proofs):
                if reject:
                    return None
                parsed = json.loads(payload)
                filename = tmp_path / (digest(payload).removeprefix("sha256:") + ".json")
                with filename.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                fd = os.open(tmp_path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                retained.append(payload)
                return {"run_id": parsed["run_id"], "output_sha256": digest(payload)}

            probe = AppProcessTLSProbe(reg, credential_provider=lambda ctx: password,
                                       binding_reader=lambda ctx: digest(encoded(reg.binding())), evidence_sink=sink)
            # Wait for the fixed SELECT 1 positive path, not only container startup.
            for attempt in range(30):
                try:
                    with psycopg.connect(host="pooler.test", hostaddr="127.0.0.1", port=port,
                                          dbname="chat", user="chat_app", password=password,
                                          sslmode="verify-full", sslrootcert=str(tmp_path / "server.crt"),
                                          connect_timeout=2) as connection:
                        assert connection.execute("SELECT 1").fetchone() == (1,)
                    break
                except psycopg.OperationalError:
                    if attempt == 29:
                        raise RuntimeError("local positive control unavailable") from None
                    time.sleep(.5)
            tests = ("ENC-01.1", "ENC-01.2", "ENC-01.3")
            run = Runner(catalog, probes=dict.fromkeys(tests, probe)).run(
                profile_id="full", profile_version="1", app="demo-chat", environment=environment)
            rows = [row for row in run["results"] if row["test_id"] in tests]
            assert [row["verdict"] for row in rows] == ["UNKNOWN" if rejected else "PASS"] * 3
            assert all(row["unknown_reason"] == "NOT_RUN" for row in run["results"] if row not in rows)
            assert len(proofs) == (0 if rejected else 3)
            assert password not in json.dumps(run)
            assert all(password.encode() not in proof for proof in proofs)
        class PlaintextOverride(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                initial = b""
                while len(initial) < 8:
                    part = self.request.recv(8 - len(initial))
                    if not part:
                        return
                    initial += part
                if initial == struct.pack("!II", 8, 80877103):
                    # Preserve the real TLS/auth/SELECT path byte-for-byte.
                    with socket.create_connection(("127.0.0.1", port), timeout=5) as upstream:
                        upstream.sendall(initial)
                        try:
                            while True:
                                readable, _, _ = select.select([self.request, upstream], [], [], 5)
                                if not readable:
                                    return
                                for source in readable:
                                    chunk = source.recv(8192)
                                    if not chunk:
                                        return
                                    (upstream if source is self.request else self.request).sendall(chunk)
                        except OSError:
                            return
                else:
                    length, version = struct.unpack("!II", initial)
                    if version != 196608 or not 8 <= length <= 4096:
                        return
                    body = b""
                    while len(body) < length - 8:
                        part = self.request.recv(length - 8 - len(body))
                        if not part:
                            return
                        body += part
                    self.server.startups.append(initial + body)
                    self.request.sendall(self.server.plain_response)

        class Proxy(socketserver.ThreadingTCPServer):
            daemon_threads = True

        cases = [
            (b"E", b"SFATAL\0MSSL required\0\0C08P01\0\0", "UNKNOWN"),
            (b"R", struct.pack("!I", 0xffffffff), "UNKNOWN"),
            (b"E", b"SFATAL\0C08P01\0MSSL required\0\0", "PASS"),
            (b"R", struct.pack("!I", 3), "FAIL"),
        ]
        for kind, body, expected in cases:
            with Proxy(("127.0.0.1", 0), PlaintextOverride) as proxy:
                proxy.plain_response = kind + struct.pack("!I", len(body) + 4) + body
                proxy.startups = []
                thread = Thread(target=proxy.serve_forever, daemon=True)
                thread.start()
                try:
                    changed = replace(reg, target=replace(target, port=proxy.server_address[1]))
                    retained = []

                    def proxy_sink(raw, saved=retained):
                        saved.append(json.loads(raw))
                        return sink(raw, reject=False)

                    probe = AppProcessTLSProbe(changed, credential_provider=lambda ctx: password,
                        binding_reader=lambda ctx, active=changed: digest(encoded(active.binding())),
                        evidence_sink=proxy_sink)
                    run = Runner(catalog, {"ENC-01.3": probe}).run(profile_id="full", profile_version="1",
                                                               app="demo-chat", environment=environment)
                    row = next(row for row in run["results"] if row["test_id"] == "ENC-01.3")
                    assert row["verdict"] == expected
                    assert row["positive_control"] == "pass"
                    assert row["unknown_reason"] == ("UNEXPECTED_RESPONSE" if expected == "UNKNOWN" else None)
                    assert len(retained) == len(proxy.startups) == 1
                    assert password.encode() not in proxy.startups[0]
                    assert retained[0]["checks"][0]["outcome"] == retained[0]["checks"][-1]["outcome"] == "allow"
                finally:
                    proxy.shutdown()
                    thread.join(timeout=5)
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)
        for name in ("server.key", "userlist.txt"):
            (tmp_path / name).unlink(missing_ok=True)
