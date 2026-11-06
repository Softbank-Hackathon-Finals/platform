"""Public, version-aware reader of one stored run (T21 console, review #270).

``read_run`` (S3) and ``read_local_run`` (``FileSink`` directory) return a ``VerifiedRun`` or raise
``RunRefused`` with a fixed ``code``; they never return partially checked records and never put
store messages, bucket names or record text in the error. Reading only lists and gets: the T24
reader role is enough.

S3 checks, in order: every version and delete marker under ``<prefix>/<plan>/<run>/`` is listed
and a record key with more than one version (``overwritten``) or a delete marker
(``delete_marker``) is refused; each record must sit under its own plan/run and key
(``record_outside_its_run``); the chain must verify (``invalid_record`` / ``mixed_runs`` /
``sequence_gap`` / ``chain_broken``); and, when a registration is given, the stored chain must
contain it (``registration_mismatch``).

A registration anchors a prefix of the run: record ``record_count`` must exist and its sha256 must
be ``chain_digest``. The run may have grown after the registration was taken (a follower keeps
writing), so later records are counted in ``unregistered_records`` but **not returned**; a host
shows only what its registration vouches for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .ledger import chain_digest, verify_chain
from .registration import InvalidRegistration, check_registration
from .storage import IDENTIFIER, StoreTampered, _local_run_items, _s3_run_items

READ_CODES = frozenset({
    "invalid_identifier", "store_unreadable", "unreadable_record", "run_not_found",
    "overwritten", "delete_marker", "record_outside_its_run",
    "invalid_record", "mixed_runs", "sequence_gap", "chain_broken",
    "registration_invalid", "registration_mismatch",
})


class RunRefused(Exception):
    """The run cannot be shown as verified. ``code`` is one of ``READ_CODES``; ``sequence`` is
    the record it concerns, when there is one."""

    def __init__(self, code: str, sequence: int | None = None):
        assert code in READ_CODES, code
        self.code, self.sequence = code, sequence
        super().__init__(code if sequence is None else f"{code}:{sequence}")


@dataclass(frozen=True)
class VerifiedRun:
    plan_id: str
    run_id: str
    records: tuple[dict, ...]  # sequence 1..record_count, chain verified
    record_count: int
    chain_digest: str  # sha256 of the last returned record
    registered: bool  # True when checked against a registration (records end at its anchor)
    unregistered_records: int = 0  # stored after the registration's anchor; not returned


def _refused(reason: str) -> RunRefused:
    code, _, sequence = reason.partition(":")
    if code not in READ_CODES:
        code = "invalid_record"
    return RunRefused(code, int(sequence) if sequence.isdigit() else None)


def _ids(plan_id: str, run_id: str) -> None:
    if not all(isinstance(v, str) and IDENTIFIER.match(v) for v in (plan_id, run_id)):
        raise RunRefused("invalid_identifier")


def verify_records(plan_id: str, run_id: str, items: list[tuple[str | None, dict]],
                   registration: dict | None = None) -> VerifiedRun:
    """Check (record file name, record) pairs of one run and anchor them to ``registration``.
    A name of None (records not read from a store, e.g. JSON Lines) skips only the key check."""
    if registration is not None:
        try:
            registration = check_registration(registration)
        except InvalidRegistration:
            raise RunRefused("registration_invalid") from None
        if (registration["plan_id"], registration["run_id"]) != (plan_id, run_id):
            raise RunRefused("registration_mismatch")
    records = [doc for _name, doc in items]
    if not records:
        # Under Object Lock a registered run cannot vanish without leaving versions/markers.
        raise RunRefused("registration_mismatch" if registration else "run_not_found")
    ok, reason = verify_chain(records)
    if not ok:
        raise _refused(reason)
    for name, doc in items:
        if ((doc["plan_id"], doc["run_id"]) != (plan_id, run_id)
                or name is not None and name != f"{doc['sequence']:08d}.json"):
            raise RunRefused("record_outside_its_run", doc["sequence"])
    if registration is None:
        return VerifiedRun(plan_id, run_id, tuple(records), len(records), chain_digest(records), False)
    count = registration["record_count"]
    if count > len(records) or records[count - 1]["sha256"] != registration["chain_digest"]:
        raise RunRefused("registration_mismatch")
    return VerifiedRun(plan_id, run_id, tuple(records[:count]), count, registration["chain_digest"], True,
                       len(records) - count)


def read_run(client, bucket: str, prefix: str, plan_id: str, run_id: str, *,
             registration: dict | None = None) -> VerifiedRun:
    """Read and verify ``s3://<bucket>/<prefix>/<plan_id>/<run_id>/`` (``prefix`` is the sink
    root, ``audit`` by default for the sink). Raises ``RunRefused``."""
    _ids(plan_id, run_id)
    root = prefix.strip("/")
    run_prefix = f"{root}/{plan_id}/{run_id}/" if root else f"{plan_id}/{run_id}/"
    try:
        items = _s3_run_items(client, bucket, run_prefix)
    except StoreTampered as exc:
        raise _refused(str(exc)) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RunRefused("unreadable_record") from None
    except Exception:  # noqa: BLE001 - client/network errors carry bucket names; only the code leaves
        raise RunRefused("store_unreadable") from None
    return verify_records(plan_id, run_id, [(key.rsplit("/", 1)[-1], doc) for key, doc in items], registration)


def read_local_run(root: str | Path, plan_id: str, run_id: str, *, prefix: str = "audit",
                   registration: dict | None = None) -> VerifiedRun:
    """Same as ``read_run`` for a ``FileSink`` root (``<root>/<prefix>/<plan>/<run>/``). A local
    directory keeps no versions, so ``overwritten``/``delete_marker`` cannot be detected here;
    use it for tests and demos, the S3 reader for evidence."""
    _ids(plan_id, run_id)
    path = Path(root) / prefix.strip("/") / plan_id / run_id
    try:
        items = _local_run_items(path) if path.is_dir() else []
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RunRefused("unreadable_record") from None
    except OSError:
        raise RunRefused("store_unreadable") from None
    return verify_records(plan_id, run_id, items, registration)
