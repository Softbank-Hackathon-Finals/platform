"""Run registration: the anchor a host keeps *outside* the audit bucket (review #270).

A reader that only lists the bucket trusts whatever the bucket holds. A writer that can put new
objects there could replace a whole run with a consistently re-hashed chain on fresh keys or a
fresh prefix, and the chain alone would verify. The registration is built from the writer's own
receipts (what it wrote and read back), never from a listing, so a host that stores it where the
audit writer cannot change it can hand it to ``read_run`` as the expected anchor.

``{schema_version, plan_id, run_id, record_count, chain_digest, last_sequence, written_at}``:
``chain_digest`` is the sha256 of the last record written, ``last_sequence`` its sequence. A run's
sequences are contiguous from 1, so ``record_count == last_sequence``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from .ledger import utc_now
from .storage import IDENTIFIER

REGISTRATION_SCHEMA_VERSION = "1.0"
REGISTRATION_FIELDS = ("schema_version", "plan_id", "run_id", "record_count", "chain_digest",
                       "last_sequence", "written_at")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_RECORD_NAME = re.compile(r"([0-9]{8})\.json\Z")


class InvalidRegistration(ValueError):
    """The document is not a registration v1 (or a receipt does not bind the run)."""


def registration_from_receipt(plan_id: str, run_id: str, receipt: dict, written_at: str | None = None) -> dict:
    """Registration after a confirmed write. ``receipt`` is ``Receipt.as_dict()`` of the *last*
    record written for the run (``sink.put`` returned it only after readback)."""
    key, digest = receipt.get("key"), receipt.get("sha256")
    parts = key.split("/") if isinstance(key, str) else []
    match = _RECORD_NAME.fullmatch(parts[-1]) if parts else None
    if len(parts) < 4 or parts[-3:-1] != [plan_id, run_id] or not match:
        raise InvalidRegistration("receipt key does not belong to the run")
    sequence = int(match.group(1))
    return check_registration({"schema_version": REGISTRATION_SCHEMA_VERSION, "plan_id": plan_id,
                               "run_id": run_id, "record_count": sequence, "chain_digest": digest,
                               "last_sequence": sequence, "written_at": written_at or utc_now()})


def check_registration(doc) -> dict:
    """Return ``doc`` if it is a well-formed registration v1, else raise ``InvalidRegistration``."""
    if not isinstance(doc, dict) or set(doc) != set(REGISTRATION_FIELDS):
        raise InvalidRegistration("registration fields")
    count, last = doc["record_count"], doc["last_sequence"]
    if (doc["schema_version"] != REGISTRATION_SCHEMA_VERSION
            or not all(isinstance(doc[k], str) and IDENTIFIER.match(doc[k]) for k in ("plan_id", "run_id"))
            or type(count) is not int or type(last) is not int or not 1 <= count <= 999_999_999 or last != count
            or not isinstance(doc["chain_digest"], str) or not _SHA256.match(doc["chain_digest"])
            or not isinstance(doc["written_at"], str) or not _TIMESTAMP.match(doc["written_at"])):
        raise InvalidRegistration("registration values")
    return doc


def write_registration(path: Path, registration: dict) -> None:
    """Replace ``path`` atomically (0600). A reader never sees a half-written registration."""
    path = Path(path)
    data = json.dumps(check_registration(registration), ensure_ascii=False, sort_keys=True).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_registration(path: Path) -> dict:
    try:
        return check_registration(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise InvalidRegistration("registration unreadable") from exc
