"""Operator execution ledger: one durable send record and recipient key per request."""

import json
import os
import re
import sqlite3
import stat
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .product_execution_contracts import (
    ExecutionIntent,
    ExecutionUnknown,
    validate_execution_receipt,
)
from .subscription_product_seal import open_receipt


def _private_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)


class OperatorLedger:
    """T10 owns the admission claim. This ledger owns only its external send receipt.

    One row per request before any carrier effect, and a per-request recipient key: repeating
    submit, losing a response or restarting never sends again. The carrier (the platform Job)
    subclasses this; no scheduler, dispatcher, retry, credentials or model runs here.
    """

    _parse_intent = staticmethod(ExecutionIntent.model_validate)
    _open_receipt = staticmethod(open_receipt)
    _validate_receipt = staticmethod(validate_execution_receipt)

    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ValueError('private execution directory required')
        self.path = self.root / 'executions.sqlite3'
        try:
            _private_file(self.path)
        except FileExistsError:
            pass
        mode = self.path.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError('private execution ledger required')
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS executions (request_id TEXT PRIMARY KEY, intent TEXT NOT NULL, metadata TEXT NOT NULL, run_id TEXT)')

    def _db(self):
        return sqlite3.connect(self.path, timeout=15)

    def _private_key(self, request_id, *, create=False):
        if not isinstance(request_id, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', request_id) is None:
            raise ValueError('invalid execution request ID')
        path = self.root / ('recipient-' + request_id + '.key')
        if create and not path.exists():
            with self._db() as db:
                sent = db.execute('SELECT 1 FROM executions WHERE request_id=?', (request_id,)).fetchone()
            if sent:
                raise ExecutionUnknown('EXECUTION_KEY_UNAVAILABLE')
            # Publish a fully fsynced private key atomically; concurrent prepares use the winner.
            fd, temporary = tempfile.mkstemp(prefix='.recipient-', dir=self.root)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(X25519PrivateKey.generate().private_bytes_raw())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                os.unlink(temporary)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                mode = os.fstat(stream.fileno()).st_mode
                if not stat.S_ISREG(mode) or mode & 0o077:
                    raise ValueError('private key file required')
                raw = stream.read(33)
            return X25519PrivateKey.from_private_bytes(raw)
        except Exception:
            raise ExecutionUnknown('EXECUTION_KEY_UNAVAILABLE') from None

    def recipient(self, request_id):
        """Prepare a local decryption key once, returning only its public key to T10."""
        return self._private_key(request_id, create=True).public_key().public_bytes_raw().hex()

    def _record(self, intent):
        with self._db() as db:
            row = db.execute('SELECT intent, metadata, run_id FROM executions WHERE request_id=?', (intent.request_id,)).fetchone()
        if row is None:
            raise ExecutionUnknown('EXECUTION_NOT_SUBMITTED')
        if json.loads(row[0]) != intent.model_dump():
            raise ExecutionUnknown('EXECUTION_INTENT_CONFLICT')
        return json.loads(row[1]), row[2]
