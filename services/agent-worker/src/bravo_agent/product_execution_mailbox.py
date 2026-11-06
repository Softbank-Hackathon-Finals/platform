"""One private, immutable file handoff between T10 and its authenticated operator.

The operator prepares public metadata before T10's plan transaction. Only T10's
submit publishes an intent, after its durable claim. The operator transports a
provider-verified receipt back over its existing authenticated execution window.
This module has no cloud credentials, network calls, dispatcher or retry loop.
"""

import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from .product_execution_contracts import (
    ExecutionIntent,
    ExecutionPending,
    ExecutionUnknown,
    validate_execution_receipt,
)
from .subscription_product_seal import MAX_PLAINTEXT, validate_recipient

MAX_INTENT = 2048


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('nonfinite JSON value')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def decode(raw):
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)


class FileProductExecution:
    """T10 client port for one preconfigured intent in an owner-only directory.

    Provision the directory as the actual T10 process UID, mode 0700. Preparation
    and receipt delivery belong to the authenticated operator, not HTTP callers.
    Those operations can run under that same UID via the reviewed SSM carrier.
    Neither method authorizes an Admission; T10 rechecks it before and after use.
    """

    _parse_intent = staticmethod(ExecutionIntent.model_validate)

    def __init__(self, root):
        self.root = Path(root)
        with self._directory() as fd:
            info = os.fstat(fd)
            self.identity = info.st_dev, info.st_ino

    @contextmanager
    def _directory(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (info.st_uid != os.geteuid() or info.st_mode & 0o077
                    or getattr(self, 'identity', (info.st_dev, info.st_ino)) != (info.st_dev, info.st_ino)):
                raise ExecutionUnknown('MAILBOX_DIRECTORY_UNCONFIRMED')
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _read_at(directory, name, limit):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > limit):
                raise ExecutionUnknown('MAILBOX_FILE_UNCONFIRMED')
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ExecutionUnknown('MAILBOX_FILE_UNCONFIRMED')
        return decode(raw)

    def _read(self, name, limit):
        with self._directory() as directory:
            return self._read_at(directory, name, limit)

    def _publish(self, name, value, limit):
        raw = encode(value)
        if len(raw) > limit:
            raise ExecutionUnknown('MAILBOX_FILE_TOO_LARGE')
        with self._directory() as directory:
            temporary = '.pending-' + secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    if encode(self._read_at(directory, name, limit)) != raw:
                        raise ExecutionUnknown('MAILBOX_IMMUTABLE_CONFLICT') from None
                os.fsync(directory)
            finally:
                os.unlink(temporary, dir_fd=directory)

    def prepare(self, intent):
        """Operator-only: install public metadata; does not publish work to run."""
        intent = self._parse_intent(intent)
        validate_recipient(intent.recipient_public_key)
        self._publish('prepared.json', intent.model_dump(), MAX_INTENT)

    def prepared(self):
        return self._parse_intent(self._read('prepared.json', MAX_INTENT))

    def recipient(self, request_id):
        """Local read only; safe inside the T10 plan lock, no key generation."""
        prepared = self.prepared()
        if prepared.request_id != request_id:
            raise ExecutionUnknown('MAILBOX_REQUEST_CONFLICT')
        return prepared.recipient_public_key

    def _bound(self, intent):
        intent = self._parse_intent(intent)
        if intent != self.prepared():
            raise ExecutionUnknown('MAILBOX_INTENT_CONFLICT')
        return intent

    def submit(self, intent):
        intent = self._bound(intent)
        self._publish('intent.json', intent.model_dump(), MAX_INTENT)
        return self.read(intent)

    def claimed(self):
        """Operator read: a prepared key alone is never dispatchable work."""
        try:
            return self._bound(self._read('intent.json', MAX_INTENT))
        except FileNotFoundError:
            raise ExecutionUnknown('MAILBOX_INTENT_NOT_PUBLISHED') from None

    @staticmethod
    def _receipt(intent, value):
        try:
            run_id = value['github_run_id']
            if not isinstance(run_id, str) or re.fullmatch(r'[1-9][0-9]{0,19}', run_id) is None:
                raise ValueError()
            return validate_execution_receipt(intent, run_id, value)
        except Exception:
            raise ExecutionUnknown('MAILBOX_RECEIPT_UNCONFIRMED') from None

    def read(self, intent):
        intent = self._bound(intent)
        if intent != self.claimed():
            raise ExecutionUnknown('MAILBOX_INTENT_CONFLICT')
        try:
            value = self._read('receipt.json', MAX_PLAINTEXT)
        except FileNotFoundError:
            raise ExecutionPending('MAILBOX_RECEIPT_PENDING') from None
        return self._receipt(intent, value)

    def deliver(self, intent, receipt):
        """Operator-only: publish one verified receipt, never replace a result."""
        intent = self._bound(intent)
        if intent != self.claimed():
            raise ExecutionUnknown('MAILBOX_INTENT_CONFLICT')
        self._receipt(intent, receipt)
        self._publish('receipt.json', receipt, MAX_PLAINTEXT)
