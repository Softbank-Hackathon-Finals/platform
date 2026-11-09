"""Real T16 documents and vetted source reads; no model/cloud execution."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from bravo_agent.contracts import SnapshotManifest
from bravo_agent.tools import Snapshot
from bravo_inventory import build_inventory

from bravo_platform_events.initial_proposal import InitialProposalRecords
from bravo_platform_events.initial_source import InitialSnapshotSource, inventory_analysis
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import Analysis

SOURCE = "b" * 40


@pytest.fixture
def inventory(tmp_path):
    root = tmp_path / "source"
    (root / "app").mkdir(parents=True)
    content = 'import boto3\nclient = boto3.client("secretsmanager")\nclient.get_secret_value(SecretId="demo")\n'
    (root / "app/db_secret.py").write_text(content)
    doc = build_inventory(app_id="demo-chat", repo=root, app_path="app",
        source_env={"env_id": "source", "env_kind": "aws", "namespace": "demo"},
        adapter=None, profiles=[], source_sha=SOURCE, now=datetime(2026, 11, 6, tzinfo=UTC))
    item_id = next(item["id"] for item in doc["items"] if item["role"] == "secrets")
    manifest = SnapshotManifest(source_sha=SOURCE,
        files={"app/db_secret.py": hashlib.sha256(content.encode()).hexdigest()},
        editable_paths=["app/db_secret.py"])
    return doc, item_id, Snapshot(root, manifest)


def reseal(document):
    raw = json.dumps({k: v for k, v in document.items() if k not in {"digest", "generated_at"}},
                     sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    document["digest"] = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def test_t16_original_selection_is_preserved(inventory):
    doc, item_id, _ = inventory
    before = deepcopy(doc)
    selected = inventory_analysis(doc, source_sha=SOURCE, item_id=item_id)
    assert selected.grade == "code_change"
    assert selected.rationale == next(i["rationale"] for i in doc["items"] if i["id"] == item_id)
    assert selected.inventory_digest == doc["digest"][7:]
    assert selected.item_id == item_id and doc == before


@pytest.mark.parametrize("mutation", ["content", "missing_digest", "prefix", "source", "missing_item",
    "duplicate_item", "empty_summary", "long_summary", "schema", "unknown", "none", "adapter", "config_swap"])
def test_invalid_or_unagreed_inventory_is_refused(inventory, mutation):
    doc, item_id, _ = inventory
    selected = next(i for i in doc["items"] if i["id"] == item_id)
    if mutation == "content":
        selected["rationale"] = "different verified content"
    elif mutation == "missing_digest":
        del doc["digest"]
    elif mutation == "prefix":
        doc["digest"] = doc["digest"][7:]
    elif mutation == "source":
        doc["app"]["source_sha"] = "f" * 40
    elif mutation == "missing_item":
        item_id = "item-999"
    elif mutation == "duplicate_item":
        doc["items"].append(deepcopy(selected))
    elif mutation == "empty_summary":
        selected["rationale"] = "   "
    elif mutation == "long_summary":
        selected["rationale"] = "x" * 4097
    elif mutation == "schema":
        doc["schema_version"] = "unsupported"
    else:
        selected["grade"] = mutation
    if mutation not in {"content", "missing_digest", "prefix"}:
        reseal(doc)
    with pytest.raises(ValueError, match="^initial_inventory_unavailable$"):
        inventory_analysis(doc, source_sha=SOURCE, item_id=item_id)


def test_generated_at_is_preserved_but_not_part_of_t16_identity(inventory):
    doc, item_id, _ = inventory
    doc["generated_at"] = "2026-11-06T01:00:00Z"
    assert inventory_analysis(doc, source_sha=SOURCE, item_id=item_id).inventory_digest == doc["digest"][7:]
    assert doc["generated_at"] == "2026-11-06T01:00:00Z"


def test_real_t16_source_is_bound_to_current_analysis_and_grant(backend, tmp_path, inventory):
    from types import SimpleNamespace

    from test_cutover import SPEC
    from test_initial_proposal import ACTOR, Fixture
    from test_workflow_recovery import ANALYSIS

    doc, item_id, snapshot = inventory
    source = InitialSnapshotSource(snapshot, lambda *args: doc, item_id)
    initial = source(None, {}, SimpleNamespace(source_sha=SOURCE))
    analysis = Analysis(ANALYSIS.scope_digest, doc["digest"][7:], ANALYSIS.comparison_digest)
    f = Fixture(backend, tmp_path, source_value=initial, analysis=analysis)
    records = InitialProposalRecords(backend.store, catalog=f.catalog, source=source,
                                     authorize=f.access.require, clock=lambda: f.now)
    value = records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref)
    assert value["schema_version"] == 2 and value["analysis"] == initial["analysis"]
    assert value["binding"]["stage"] == "PROPOSE" and "admission" not in value
    path = snapshot.root / "app/db_secret.py"
    original = path.read_text()
    path.write_text("VALUE = 9\n")
    with pytest.raises(EventError):
        records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref)
    path.write_text(original)
    old = deepcopy(doc)
    doc["inputs"]["repo_digest"] = "sha256:" + "f" * 64
    reseal(doc)
    with pytest.raises(EventError):
        records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref)
    doc.clear()
    doc.update(old)
    f.grant(False, 1)
    with pytest.raises(EventError):
        records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref)
    assert not f.client.submits


@pytest.mark.parametrize("change", ["item", "summary", "missing_analysis", "version"])
def test_initial_selection_cannot_change_under_saved_input(backend, tmp_path, change):
    from test_initial_proposal import Fixture

    f = Fixture(backend, tmp_path)
    if change == "item":
        f.source_value["analysis"]["item_id"] = "item-002"
    elif change == "summary":
        f.source_value["analysis"]["rationale"] = "Another selected objective"
    elif change == "missing_analysis":
        del f.source_value["analysis"]
    else:
        f.source_value["schema_version"] = 1
    with pytest.raises(EventError):
        f.execute()
    assert not f.client.submits and not f.client.keys
