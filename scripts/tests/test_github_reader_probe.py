import base64
import hashlib
import importlib.util
import unittest
from datetime import UTC, datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location("reader", Path(__file__).parents[1] / "github_reader_probe.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
TOKEN = "github_pat_" + "x" * 24
NOW = datetime(2026, 11, 5, tzinfo=UTC)


def responses():
    data = b"Pinned example source\n"
    return [{"message": "Requires authentication"}, {"id": reader.ACTOR_ID},
            {"id": reader.REPOSITORY_ID, "full_name": reader.REPOSITORY}, {"sha": reader.REVISION},
            {"type": "file", "path": reader.SOURCE_PATH, "encoding": "base64",
             "content": base64.b64encode(data).decode(), "size": len(data),
             "sha": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()}]


class ReaderProbeTests(unittest.TestCase):
    def test_authenticated_pinned_source_and_no_write_surface(self):
        queue, calls = responses(), []

        def call(token, path, expected):
            calls.append((token, path, expected))
            return queue.pop(0)

        receipt = reader.verify(TOKEN, call, now=NOW)
        self.assertEqual(calls[0], (None, "/user", 401))
        self.assertTrue(all(token == TOKEN for token, _, _ in calls[1:]))
        self.assertTrue(calls[-1][1].endswith("?ref=" + reader.REVISION))
        self.assertEqual(receipt["github_write_requests"], 0)
        self.assertGreater(receipt["source_bytes"], 0)
        self.assertNotIn(TOKEN, str(receipt))

    def rejected(self, index, key, value, category):
        queue = responses()
        queue[index][key] = value
        with self.assertRaisesRegex(reader.ProbeError, category):
            reader.verify(TOKEN, lambda *_: queue.pop(0), now=NOW)

    def test_wrong_actor(self):
        self.rejected(1, "id", 1, "authenticated_actor_mismatch")

    def test_wrong_repository(self):
        self.rejected(2, "id", 1, "repository_mismatch")

    def test_wrong_revision(self):
        self.rejected(3, "sha", "0" * 40, "revision_mismatch")

    def test_wrong_source_path(self):
        self.rejected(4, "path", "other.md", "source_metadata_mismatch")

    def test_wrong_source_bytes(self):
        self.rejected(4, "sha", "0" * 40, "source_blob_mismatch")

    def test_expired_credential_is_rejected_before_network(self):
        def no_request(*_):
            self.fail("expired credential reached network")

        with self.assertRaisesRegex(reader.ProbeError, "credential_expired"):
            reader.verify(TOKEN, no_request, now=reader.EXPIRY)
