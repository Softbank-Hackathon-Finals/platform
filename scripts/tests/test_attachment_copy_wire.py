"""What real libpq sends to a server that asks for a cleartext password. No container is needed.

Needs psycopg, so default discovery skips it. Run it explicitly:

    uv run --no-project --python 3.12 --with 'psycopg[binary]==3.3.6' python -m unittest \
        discover -s scripts/tests -p test_attachment_copy_wire.py -v
"""
import contextlib
import importlib.util
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

WORKLOADS = Path(os.environ.get("T8_ATTACH_MODULE_DIR")
                 or Path(__file__).resolve().parents[2] / "infra/envs/target-workloads")
# Not a credential: it exists only to be searched for in the bytes the fake server received.
CANARY = "wire-canary-not-a-credential"
LOGIN = f"user=copy password={CANARY} dbname=app"
SOCKET_PORT = 5999
SSL_REQUEST, GSS_REQUEST = 80877103, 80877104
ASK_CLEARTEXT_PASSWORD = b"R" + struct.pack("!II", 8, 3)
ACCEPT_AND_READY = b"R" + struct.pack("!II", 8, 0) + b"Z" + struct.pack("!I", 5) + b"I"
OPT_IN = {"allow_loopback_plaintext": True}
NOT_VERIFIED = "database connection must use verify-full TLS"
NOT_ONE_TARGET = "database target must be one host from the DSN or PG* environment"
attachment_copy = psycopg = None


def setUpModule():
    global attachment_copy, psycopg
    if importlib.util.find_spec("psycopg") is None:
        raise unittest.SkipTest("needs psycopg; see the module docstring")
    sys.path.append(str(WORKLOADS))
    import attachment_copy as copy_module
    import psycopg as driver
    attachment_copy, psycopg = copy_module, driver


class FakePostgres:
    """Declines encryption, asks for a cleartext password and records every byte a client sends."""

    def __init__(self, listener):
        self.listener = listener
        self.connections = 0
        self.received = bytearray()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            with client, contextlib.suppress(OSError):
                self.answer(client)

    def read(self, client, size):
        data = b""
        while len(data) < size:
            chunk = client.recv(size - len(data))
            if not chunk:
                raise ConnectionError
            self.received += chunk
            data += chunk
        return data

    def read_message(self, client):
        (length,) = struct.unpack("!I", self.read(client, 4))
        return self.read(client, length - 4)

    def answer(self, client):
        while True:
            (code,) = struct.unpack("!I", self.read_message(client)[:4])
            if code not in (SSL_REQUEST, GSS_REQUEST):
                break
            client.sendall(b"N")
        client.sendall(ASK_CLEARTEXT_PASSWORD)
        self.read(client, 1)
        self.read_message(client)
        client.sendall(ACCEPT_AND_READY)
        # Drop the client at its first query: this server only exists to receive a login.
        self.read(client, 1)


class CredentialTimingTests(unittest.TestCase):
    def setUp(self):
        # libpq reads these, so a developer's shell must not change what is measured.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in [name for name in os.environ if name.startswith("PG")]:
            del os.environ[name]

    def tcp_server(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        return FakePostgres(listener), listener.getsockname()[1]

    def socket_server(self):
        # A Unix socket path is limited to about 100 bytes; the session temp directory can exceed it.
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(directory.cleanup)
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(f"{directory.name}/.s.PGSQL.{SOCKET_PORT}")
        listener.listen()
        return FakePostgres(listener), directory.name

    def service_file(self, name, *settings):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as stream:
            stream.write("\n".join([f"[{name}]", *settings]) + "\n")
        self.addCleanup(os.unlink, stream.name)
        return stream.name

    def assert_refused_before_contact(self, server, reason, dsn="", **options):
        outcome = None
        try:
            attachment_copy.connect_read_only(dsn, **options)
        except Exception as error:  # noqa: BLE001 - the password check below must report first.
            outcome = error
        self.assertNotIn(CANARY.encode(), bytes(server.received), "the password reached the server")
        self.assertIsInstance(outcome, attachment_copy.AttachmentCopyFailed)
        self.assertEqual(outcome.reason, reason)
        self.assertEqual(server.connections, 0, "the server was contacted")

    def test_control_the_fake_server_receives_a_password_when_policy_allows_it(self):
        # Without this control, every "no password arrived" below could mean a broken fake.
        server, port = self.tcp_server()
        with contextlib.suppress(psycopg.Error):
            attachment_copy.connect_read_only(f"host=127.0.0.1 port={port} sslmode=disable {LOGIN}", **OPT_IN)
        self.assertIn(CANARY.encode(), bytes(server.received))

    def test_remote_name_on_a_local_address_from_the_dsn(self):
        server, port = self.tcp_server()
        self.assert_refused_before_contact(
            server, NOT_VERIFIED, f"host=database.invalid hostaddr=127.0.0.1 port={port} sslmode=disable {LOGIN}")

    def test_remote_settings_from_the_environment_with_an_empty_dsn(self):
        server, port = self.tcp_server()
        os.environ.update(PGHOST="database.invalid", PGHOSTADDR="127.0.0.1", PGPORT=str(port), PGSSLMODE="disable",
                          PGUSER="copy", PGPASSWORD=CANARY, PGDATABASE="app")
        self.assert_refused_before_contact(server, NOT_VERIFIED)

    def test_environment_without_sslmode_uses_the_weak_default(self):
        server, port = self.tcp_server()
        os.environ.update(PGHOST="database.invalid", PGHOSTADDR="127.0.0.1", PGPORT=str(port), PGUSER="copy",
                          PGPASSWORD=CANARY, PGDATABASE="app")
        self.assert_refused_before_contact(server, NOT_VERIFIED)

    def test_dsn_sslmode_outranks_a_safe_environment(self):
        server, port = self.tcp_server()
        os.environ["PGSSLMODE"] = "verify-full"
        self.assert_refused_before_contact(
            server, NOT_VERIFIED, f"host=database.invalid hostaddr=127.0.0.1 port={port} sslmode=disable {LOGIN}")

    def test_loopback_address_is_not_an_implicit_exception(self):
        server, port = self.tcp_server()
        self.assert_refused_before_contact(server, NOT_VERIFIED, f"host=127.0.0.1 port={port} sslmode=disable {LOGIN}")

    def test_opt_in_does_not_cover_a_remote_name(self):
        server, port = self.tcp_server()
        self.assert_refused_before_contact(
            server, NOT_VERIFIED, f"host=database.invalid hostaddr=127.0.0.1 port={port} sslmode=disable {LOGIN}",
            **OPT_IN)

    def test_service_named_by_the_environment(self):
        server, port = self.tcp_server()
        os.environ.update(PGSERVICE="copy", PGSERVICEFILE=self.service_file(
            "copy", "host=database.invalid", "hostaddr=127.0.0.1", f"port={port}", "sslmode=disable", "user=copy",
            f"password={CANARY}", "dbname=app"))
        self.assert_refused_before_contact(server, NOT_ONE_TARGET)

    def test_service_named_by_the_dsn(self):
        server, port = self.tcp_server()
        os.environ["PGSERVICEFILE"] = self.service_file(
            "copy", "host=database.invalid", "hostaddr=127.0.0.1", f"port={port}", "sslmode=disable", "user=copy",
            f"password={CANARY}", "dbname=app")
        self.assert_refused_before_contact(server, NOT_ONE_TARGET, "service=copy")

    def test_service_cannot_hide_behind_a_loopback_environment(self):
        # libpq ranks the service file above PG*, so reading only PG* would see a local test here.
        server, port = self.tcp_server()
        os.environ.update(PGHOST="127.0.0.1", PGSSLMODE="disable", PGSERVICE="copy", PGSERVICEFILE=self.service_file(
            "copy", "host=database.invalid", "hostaddr=127.0.0.1", f"port={port}", "user=copy", f"password={CANARY}",
            "dbname=app"))
        self.assert_refused_before_contact(server, NOT_ONE_TARGET, **OPT_IN)

    def test_unix_socket_is_refused_because_libpq_skips_tls_there(self):
        server, directory = self.socket_server()
        self.assert_refused_before_contact(
            server, NOT_ONE_TARGET, f"host={directory} port={SOCKET_PORT} sslmode=verify-full {LOGIN}")

    def test_host_list_cannot_carry_a_unix_socket(self):
        server, directory = self.socket_server()
        self.assert_refused_before_contact(
            server, NOT_ONE_TARGET, f"host={directory},database.invalid port={SOCKET_PORT} sslmode=verify-full {LOGIN}")

    def test_server_that_declines_tls_receives_no_password(self):
        # The accepted policy: libpq contacts the server, and stops when it cannot get verified TLS.
        server, port = self.tcp_server()
        with self.assertRaises(psycopg.OperationalError):
            attachment_copy.connect_read_only(
                f"host=database.invalid hostaddr=127.0.0.1 port={port} sslmode=verify-full {LOGIN}")
        self.assertEqual(server.connections, 1)
        self.assertNotIn(CANARY.encode(), bytes(server.received), "the password reached the server")


if __name__ == "__main__":
    unittest.main()
