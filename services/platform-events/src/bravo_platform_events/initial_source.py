"""Read approved source and the T16 item for the initial PROPOSE host.

The host selects the authenticated snapshot, original inventory and item. These
ports do not create an Admission, grant, model request or state transition.
"""

import hashlib
import json
from dataclasses import dataclass, field

from bravo_agent.tools import Snapshot
from bravo_inventory.report import validate_inventory

from .initial_proposal import InitialAnalysis, InitialSource


@dataclass(frozen=True)
class InitialSnapshotSource:
    """InitialProposalRecords.source using the existing vetted snapshot reader.

    The deployment authenticates the immutable checkout and manifest before
    construction. Catalog and current PROPOSE/grant checks stay in T10 records.
    """

    snapshot: Snapshot = field(repr=False)
    inventory: object = field(repr=False)
    item_id: str

    def __post_init__(self):
        if not callable(self.inventory):
            raise ValueError("initial_inventory_reader_required")

    def __call__(self, connection, plan, task):
        if (not isinstance(self.snapshot, Snapshot)
                or task.source_sha != self.snapshot.manifest.source_sha):
            raise ValueError("initial_snapshot_unavailable")
        analysis = inventory_analysis(self.inventory(connection, plan, task),
                                      source_sha=task.source_sha, item_id=self.item_id)
        value = InitialSource(schema_version=2, analysis=analysis, manifest=self.snapshot.manifest,
            files={path: self.snapshot.read(path) for path in self.snapshot.manifest.files})
        return value.model_dump(mode="json")


def inventory_analysis(document: dict, *, source_sha: str, item_id: str) -> InitialAnalysis:
    """Verify the original document and the accepted code_change selection.

    item_id is reviewed host configuration, never a model/HTTP selection. The
    InitialProposalRecords compares this digest to its current Analysis checkpoint
    and stores the selection inside the input digest. T38 owns bundle conversion.
    """
    try:
        # Detach mutable provider objects and reject non-JSON numeric values.
        document = json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))
        validate_inventory(document)
        raw = json.dumps({k: v for k, v in document.items() if k not in {"digest", "generated_at"}},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        observed = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if document.get("digest") != observed or document["app"].get("source_sha") != source_sha:
            raise ValueError()
        ids = [item["id"] for item in document["items"]]
        if type(item_id) is not str or len(ids) != len(set(ids)) or ids.count(item_id) != 1:
            raise ValueError()
        item = document["items"][ids.index(item_id)]
        if item["grade"] != "code_change" or not item["rationale"].strip():
            raise ValueError()
        return InitialAnalysis(inventory_digest=observed.removeprefix("sha256:"),
            item_id=item_id, grade=item["grade"], rationale=item["rationale"])
    except Exception:
        # Do not put inventory text, source contents or validation payloads in errors.
        raise ValueError("initial_inventory_unavailable") from None
