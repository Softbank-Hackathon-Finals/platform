"""Opt-in PG18 wire regressions. Only disposable, loopback-published databases.

T8_RESUME_WIRE=1 uv run --with pytest --with 'psycopg[binary]==3.3.6' \
    pytest infra/envs/target-workloads/testing/test_resume_replication_wire.py
"""

import concurrent.futures
import importlib
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
import types
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("T8_RESUME_WIRE") != "1", reason="opt-in disposable PostgreSQL")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
resume = importlib.import_module("resume_replication")
IMAGE = "public.ecr.aws/docker/library/postgres:18.4@sha256:a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636"


def command(*args, **kwargs):
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=45, **kwargs)
    if result.returncode:
        raise RuntimeError("disposable_fixture_command_failed")
    return result.stdout.strip()


def until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(.025)
    raise TimeoutError("fixture_condition_unmet")


@contextmanager
def databases(tmp_path, count=1):
    import psycopg
    from psycopg.conninfo import make_conninfo

    owner = uuid.uuid4().hex
    label = "bravo.t8-wire=" + owner
    password = secrets.token_urlsafe(32)
    command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=t8-wire", "-addext", "subjectAltName=DNS:localhost,DNS:source,DNS:target",
            "-keyout", str(tmp_path / "server.key"), "-out", str(tmp_path / "server.crt"))
    (tmp_path / "server.key").chmod(0o600)
    network = command("docker", "network", "create", "--label", label, "t8-wire-" + owner)
    containers, dsns, ports = [], {}, {}
    try:
        for side in ("source", "target")[:count]:
            cid = command("docker", "run", "-d", "--label", label, "--network", network,
                          "--network-alias", side, "--memory", "384m", "--cpus", "1",
                          "--tmpfs", "/var/lib/postgresql:rw,size=268435456", "-p", "127.0.0.1::5432",
                          "-e", "POSTGRES_PASSWORD", "--mount", f"type=bind,src={tmp_path},dst=/certs,readonly",
                          "--entrypoint", "sh", IMAGE, "-c",
                          "cp /certs/server.key /tmp/server.key && chown postgres /tmp/server.key && chmod 600 /tmp/server.key && exec docker-entrypoint.sh postgres -c wal_level=logical -c ssl=on -c ssl_cert_file=/certs/server.crt -c ssl_key_file=/tmp/server.key",
                          env=dict(os.environ, POSTGRES_PASSWORD=password))
            containers.append(cid)
            ports[side] = int(command("docker", "port", cid, "5432/tcp").rsplit(":", 1)[1])
            dsns[side] = make_conninfo(host="localhost", hostaddr="127.0.0.1", port=ports[side],
                                      dbname="postgres", user="postgres", password=password, sslmode="verify-full",
                                      sslrootcert=str(tmp_path / "server.crt"), connect_timeout=2)

            def ready(dsn=dsns[side]):
                try:
                    with psycopg.connect(dsn):
                        return True
                except psycopg.OperationalError:
                    return False
            until(ready, 30)
        yield dsns, ports, password
    finally:
        for cid in reversed(containers):
            assert command("docker", "inspect", "--format", '{{index .Config.Labels "bravo.t8-wire"}}', cid) == owner
            assert not any(m["Type"] == "volume" for m in json.loads(command("docker", "inspect", "--format", "{{json .Mounts}}", cid)))
            command("docker", "rm", "-fv", cid)
        assert command("docker", "network", "inspect", "--format", '{{index .Labels "bravo.t8-wire"}}', network) == owner
        command("docker", "network", "rm", network)
        assert not command("docker", "ps", "-aq", "--filter", "label=" + label)
        assert not command("docker", "network", "ls", "-q", "--filter", "label=" + label)
        (tmp_path / "server.key").unlink(missing_ok=True)


def test_wrong_database_cannot_spoof_source_identity(tmp_path):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    with databases(tmp_path) as (dsns, _, password):
        with psycopg.connect(dsns["source"], autocommit=True) as admin:
            for name in ("registered", "other_db"):
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("CREATE ROLE probe LOGIN PASSWORD {}").format(sql.Literal(password)))
        for name in ("registered", "other_db"):
            with psycopg.connect(make_conninfo(dsns["source"], dbname=name), autocommit=True) as admin:
                admin.execute("CREATE SCHEMA cutover")
                admin.execute("CREATE TABLE cutover.barrier (barrier_id uuid PRIMARY KEY, epoch bigint, created_at timestamptz DEFAULT now())")
                admin.execute("GRANT USAGE ON SCHEMA cutover TO probe")
                admin.execute("GRANT INSERT, SELECT, DELETE ON cutover.barrier TO probe")
                admin.execute("GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO probe")
        source = make_conninfo(dsns["source"], dbname="registered", user="probe")
        wrong = make_conninfo(dsns["source"], dbname="other_db", user="probe")
        identity = resume.read_one(source, "probe_status")
        with psycopg.connect(make_conninfo(dsns["source"], dbname="other_db"), autocommit=True) as admin:
            admin.execute("CREATE FUNCTION public.current_database() RETURNS name LANGUAGE sql AS $$ SELECT 'registered'::name $$")
            admin.execute("ALTER DATABASE other_db SET search_path=public,pg_catalog")
        observed = resume.read_one(wrong, "probe_status")
        assert observed["database"] == "other_db"
        marker = str(uuid.uuid4())
        with pytest.raises(resume.ProbeTargetChanged):
            resume.write_marker(wrong, resume.PROBE_INSERT, (marker, 0), identity)
        assert all(resume.read_one(d, "barrier_row", {"barrier_id": marker}) is None for d in (source, wrong))
        # The legitimate source is still usable with the same restricted role.
        resume.write_marker(source, resume.PROBE_INSERT, (marker, 0), identity)
        assert resume.read_one(source, "barrier_row", {"barrier_id": marker}) is not None
        resume.write_marker(source, resume.PROBE_DELETE, (marker, 0), identity)


class LostAnswer:
    def __init__(self, upstream):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.upstream = upstream
        self.drop, self.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.relay, daemon=True)
        self.thread.start()

    def relay(self):
        with self.listener, self.listener.accept()[0] as client, socket.create_connection(("127.0.0.1", self.upstream)) as server:
            while not self.drop.is_set():
                for origin in select.select([client, server], [], [], .01)[0]:
                    data = origin.recv(65536)
                    if not data:
                        return
                    (client if origin is server else server).sendall(data)
            client.shutdown(socket.SHUT_RDWR)
            self.release.wait(30)

    def close(self):
        self.release.set()
        self.thread.join(3)


def test_late_commit_stays_pending_until_serialized_delete_and_target_apply(tmp_path):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    with databases(tmp_path, 2) as (dsns, ports, password):
        for dsn in dsns.values():
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute("CREATE SCHEMA cutover")
                admin.execute("CREATE TABLE cutover.barrier (barrier_id uuid PRIMARY KEY, epoch bigint, created_at timestamptz DEFAULT now())")
        with psycopg.connect(dsns["source"], autocommit=True) as admin:
            admin.execute("CREATE PUBLICATION probe_pub FOR TABLE cutover.barrier")
            identity = {"system_identifier": admin.execute("SELECT system_identifier::text FROM pg_catalog.pg_control_system()").fetchone()[0], "database": "postgres"}
        with psycopg.connect(dsns["target"], autocommit=True) as target:
            remote = make_conninfo(host="source", dbname="postgres", user="postgres", password=password,
                                   sslmode="verify-full", sslrootcert="/certs/server.crt")
            target.execute(sql.SQL("CREATE SUBSCRIPTION probe_sub CONNECTION {} PUBLICATION probe_pub").format(sql.Literal(remote)))
            until(lambda: target.execute("SELECT EXISTS(SELECT FROM pg_subscription_rel WHERE srsubstate='r')").fetchone()[0])
        def row(side, marker):
            return resume.read_one(dsns[side], "barrier_row", {"barrier_id": marker})
        def write(stmt, params):
            return resume.write_marker(dsns["source"], stmt, params, identity)
        def applied():
            with psycopg.connect(dsns["target"], autocommit=True) as target:
                return target.execute("SELECT remote_lsn::text FROM pg_replication_origin_status").fetchone()[0]

        marker = str(uuid.uuid4())
        normal = resume.barrier_probe(write, lambda: row("target", marker), lambda: row("source", marker), marker,
                                       on_delivered=lambda: None, on_cleaned=lambda: None,
                                       elapsed=time.monotonic, timeout=3, poll=.02, read_target_applied=applied)
        assert normal["passed"] and normal["cleanup"] == "confirmed"
        with psycopg.connect(dsns["source"], autocommit=True) as admin:
            admin.execute("CREATE FUNCTION cutover.hold_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_catalog.set_config('lock_timeout','0',true); PERFORM pg_advisory_xact_lock(5826); RETURN NEW; END $$")
            admin.execute("CREATE CONSTRAINT TRIGGER hold_marker AFTER INSERT ON cutover.barrier DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cutover.hold_commit()")
            admin.execute("SELECT pg_advisory_lock(5826)")
            proxy = LostAnswer(ports["source"])
            try:
                work = resume.Run(types.SimpleNamespace(apply=True, no_barrier_probe=False, cleanup_marker=None,
                                                        probe_timeout=1, probe_poll=.02),
                                  {resume.SOURCE_DSN_ENV: dsns["source"], resume.TARGET_DSN_ENV: dsns["target"],
                                   resume.PROBE_DSN_ENV: dsns["source"]})
                work.clock_start = time.monotonic()
                # Only the original INSERT loses its response; reconnects reach the same server.
                work.writer = lambda source: lambda stmt, params: resume.write_marker(
                    make_conninfo(dsns["source"], port=proxy.port) if stmt == resume.PROBE_INSERT else dsns["source"],
                    stmt, params, source)
                def committing():
                    return admin.execute("SELECT EXISTS(SELECT FROM pg_stat_activity WHERE application_name='t8_resume' AND state='active' AND wait_event='advisory' AND query='COMMIT')").fetchone()[0]
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    future = pool.submit(work.send_marker, identity)
                    until(committing, 3)
                    proxy.drop.set()
                    report = future.result(12)
                    assert committing()
                    assert report["cleanup"] == "pending" and report["source_insert_commits"] is None
                    assert report["source_insert_submissions"] == 1 and "resume" in report
                    marker = report["barrier_id"]
                    assert row("source", marker) is None and row("target", marker) is None
                    admin.execute("SELECT pg_advisory_unlock(5826)")
                    until(lambda: row("source", marker) and row("target", marker), 3)
                    proxy.close()
                    def wait(condition):
                        return resume.wait_for(condition, elapsed=time.monotonic, timeout=3, poll=.02, sleep=time.sleep)
                    resume.remove_marker(write, lambda: row("target", marker), lambda: row("source", marker),
                                         report, wait, read_target_applied=applied)
                    assert report["cleanup"] == "confirmed"
                    assert row("source", marker) is None and row("target", marker) is None
            finally:
                admin.execute("SELECT pg_advisory_unlock_all()")
                proxy.close()
