"""Throwaway PostgreSQL pair: real logical replication TLS readback.

Requires Docker, openssl and psycopg. No AWS or team cluster access. Trust auth is
confined to two disposable containers on a dedicated Docker bridge with random
loopback ports; this qualifies the TLS collector, not authentication or egress isolation.
"""

import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo

from bravo_controls.replication_tls import (
    SPEC,
    PostgreSQLReplicationReader,
    ReplicationTarget,
    ReplicationTLSProbe,
)
from bravo_controls.runner import Environment, RunContext


def command(*args, input=None):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError("local replication fixture command failed")
    return result.stdout.strip()


def main():
    os.umask(0o077)

    def interrupted(*_):
        raise KeyboardInterrupt("local replication qualification interrupted")

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    name = "bravo-t14-repl-" + uuid4().hex[:10]
    # Resolve an existing image to a fixed local content ID for both instances.
    image = command("docker", "image", "inspect", "postgres:18.4", "--format", "{{.Id}}")
    created, network_id, connections = [], None, []
    summary = {}
    try:
        with tempfile.TemporaryDirectory(prefix="bravo-t14-repl-") as folder:
            root = Path(folder)
            (root / "ext.cnf").write_text("subjectAltName=DNS:localhost,DNS:publisher\nextendedKeyUsage=serverAuth\n")
            command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=Disposable replication CA", "-keyout", str(root / "ca.key"), "-out", str(root / "ca.crt"))
            command("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=publisher",
                    "-keyout", str(root / "tls.key"), "-out", str(root / "tls.csr"))
            command("openssl", "x509", "-req", "-in", str(root / "tls.csr"), "-CA", str(root / "ca.crt"),
                    "-CAkey", str(root / "ca.key"), "-CAcreateserial", "-days", "1", "-extfile", str(root / "ext.cnf"),
                    "-out", str(root / "tls.crt"))
            # CA private key stays on the host; mount only the test leaf key and public certs.
            mount = root / "tls"
            mount.mkdir()
            for f in ("ca.crt", "tls.crt", "tls.key"):
                (mount / f).write_bytes((root / f).read_bytes())
            # Docker Desktop does not publish host ports for internal networks.
            # No untrusted code runs here; only this fixed PostgreSQL fixture.
            network_id = command("docker", "network", "create", name)
            ports, addresses = [], []
            for alias in ("publisher", "subscriber"):
                cid = command("docker", "run", "-d", "--name", name + "-" + alias,
                              "--network", network_id, "--network-alias", alias,
                              "--cpus", "1", "--memory", "384m", "--pids-limit", "256",
                              "-p", "127.0.0.1::5432", "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
                              "--env", "POSTGRES_DB=chat", "--mount", f"type=bind,src={mount},dst=/fixture,readonly",
                              "--entrypoint", "/bin/sh", image, "-c",
                              "mkdir -p /tmp/tls && cp /fixture/* /tmp/tls/ && "
                              "chown -R postgres:postgres /tmp/tls && chmod 600 /tmp/tls/tls.key && "
                              "exec docker-entrypoint.sh postgres -c wal_level=logical -c ssl=on "
                              "-c ssl_cert_file=/tmp/tls/tls.crt -c ssl_key_file=/tmp/tls/tls.key")
                created.append(cid)
                for _ in range(20):
                    info = json.loads(command("docker", "inspect", cid))[0]
                    bindings = info["NetworkSettings"]["Ports"].get("5432/tcp")
                    if bindings:
                        break
                    if not info["State"]["Running"]:
                        raise RuntimeError("local PostgreSQL fixture exited before port publication")
                    time.sleep(0.25)
                if not bindings:
                    raise RuntimeError("local Docker did not publish loopback port")
                ports.append(int(bindings[0]["HostPort"]))
                addresses.append(next(iter(info["NetworkSettings"]["Networks"].values()))["IPAddress"])
            for port in ports:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        conn = psycopg.connect(host="localhost", hostaddr="127.0.0.1", port=port,
                                               dbname="chat", user="postgres", sslmode="verify-full",
                                               sslrootcert=str(root / "ca.crt"), connect_timeout=2, autocommit=True)
                        connections.append(conn)
                        break
                    except psycopg.Error:
                        if time.monotonic() > deadline:
                            raise RuntimeError("local PostgreSQL readiness timeout") from None
                        time.sleep(0.25)
            pub, dest = connections
            for c in connections:
                c.execute("CREATE TABLE public.canary (id integer PRIMARY KEY)")
            pub.execute("CREATE PUBLICATION test_pub FOR TABLE public.canary")
            config_time = datetime.now(UTC)

            def conninfo(mode):
                return make_conninfo(host="publisher", port=5432, dbname="chat", user="postgres",
                                     sslmode=mode, sslrootcert="/tmp/tls/ca.crt", application_name="test_sub")

            from psycopg import sql

            dest.execute(sql.SQL("CREATE SUBSCRIPTION test_sub CONNECTION {} PUBLICATION test_pub WITH (copy_data=false)")
                         .format(sql.Literal(conninfo("verify-full"))))
            env = Environment("source", "source", "local:postgres/publisher")
            target = ReplicationTarget("fixture", env, created[0], created[1], "chat", "chat", "test_sub",
                                       "test_sub", "postgres", "test_sub", "publisher", 5432,
                                       addresses[1], "/tmp/tls/ca.crt", "sha256:" + "a" * 64, config_time)
            context = RunContext("run-" + uuid4().hex, "fixture", env)

            def observe(current):
                reader = PostgreSQLReplicationReader(pub, dest, read_registration=lambda *_: current.binding())
                retained = []
                result = ReplicationTLSProbe(current, collector=reader, evidence_sink=retained.append)(context, SPEC)
                assert all("CONNECTION" not in x.decode() for x in retained)
                return result

            def wait_verdict(current, expected):
                deadline = time.monotonic() + 30
                while True:
                    result = observe(current)
                    if result.observed == expected:
                        return
                    if time.monotonic() >= deadline:
                        reader = PostgreSQLReplicationReader(pub, dest, read_registration=lambda *_: current.binding())
                        try:
                            reader(context, current, datetime.now(UTC) + timedelta(seconds=30))
                        except ValueError as error:
                            # Collector ValueErrors are fixed, nonsecret diagnostics.
                            raise RuntimeError("local qualification: " + str(error)) from None
                        raise RuntimeError("replication TLS qualification did not reach expected verdict")
                    time.sleep(0.25)

            wait_verdict(target, "enabled")
            pub.execute("INSERT INTO public.canary VALUES (1)")
            deadline = time.monotonic() + 20
            while dest.execute("SELECT count(*) FROM public.canary").fetchone()[0] != 1:
                if time.monotonic() > deadline:
                    raise RuntimeError("actual logical replication missing")
                time.sleep(0.25)
            summary["verify_full_live_sender"] = "PASS"
            summary["real_row_replicated"] = True
            # A future configuration epoch cannot certify an existing connection.
            assert observe(replace(target, configured_at=datetime.now(UTC))).observed is None
            summary["old_sender_rejected"] = True
            assert observe(replace(target, slot="unregistered_slot")).observed is None
            summary["unrelated_slot_rejected"] = True
            for mode in ("require", "disable"):
                applied = datetime.now(UTC)
                dest.execute(sql.SQL("ALTER SUBSCRIPTION test_sub CONNECTION {}").format(sql.Literal(conninfo(mode))))
                target = replace(target, configured_at=applied)
                wait_verdict(target, "disabled")
                summary["live_" + mode] = "FAIL"
            dest.execute("ALTER SUBSCRIPTION test_sub DISABLE")
            assert observe(target).observed is None
            summary["disabled_subscription"] = "UNKNOWN"
            assert pub.execute("SELECT count(*) FROM public.canary").fetchone()[0] == 1
            assert dest.execute("SELECT count(*) FROM public.canary").fetchone()[0] == 1
            summary["inspection_data_unchanged"] = True
    finally:
        cleanup_errors = []
        for c in connections:
            try:
                c.close()
            except Exception:
                cleanup_errors.append("connection")
        for cid in reversed(created):
            try:
                command("docker", "rm", "-f", "-v", cid)
            except Exception:
                cleanup_errors.append("container")
        if network_id:
            try:
                command("docker", "network", "rm", network_id)
            except Exception:
                cleanup_errors.append("network")
        signal.signal(signal.SIGTERM, previous_handler)
        if cleanup_errors:
            raise RuntimeError("owned fixture cleanup incomplete: " + ",".join(cleanup_errors))
    summary["owned_resources_removed"] = True
    summary["image_id"] = image
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
