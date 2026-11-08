"""Inventory contract v1 (docs/contracts/T16-inventory-v1.md): producer output and consumer copies agree."""

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from bravo_inventory import build_inventory, validate_inventory
from bravo_inventory.deploy_trace import FileAdapter
from bravo_inventory.report import SCHEMA, _digest

SERVICES = Path(__file__).resolve().parents[2]
# Consumer tests that read a stored T16 document instead of running the scanner.
CONSUMER_COPIES = [
    "agent-worker/tests/fixtures/t16-inventory-demo-chat.json",
    "exit-drill/tests/fixtures/inventory-demo-chat.json",
    "exit-drill/tests/fixtures/inventory-t16-shakedown.json",
    "target-recommend/tests/fixtures/t16-inventory-demo-chat.json",
]


def _doc(repo, exports, profiles, tmp_path):
    api = tmp_path / "onprem"
    api.mkdir()
    for f in (exports.parent / "onprem").iterdir():
        (api / f.name).write_bytes(f.read_bytes())
    (api / "version.json").write_text(json.dumps({"gitVersion": "v1.31.4+rke2r1"}), encoding="utf-8")
    return build_inventory(app_id="demo-chat", repo=repo, app_path="app",
                           source_env={"env_id": "aws-seoul", "env_kind": "aws", "namespace": "chat"},
                           adapter=FileAdapter(exports), profiles=profiles, exports={"onprem-demo": api},
                           now=datetime(2026, 11, 8, tzinfo=UTC))


def test_real_output_with_detected_capabilities_passes_the_schema(repo, exports, profiles, tmp_path):
    doc = _doc(repo, exports, profiles, tmp_path)
    validate_inventory(doc)
    caps = {c["env_id"]: c["capabilities"] for c in doc["candidates"]}["onprem-demo"]
    assert caps["k8s_version"] == {"value": "v1.31.4+rke2r1", "source": "observed: /version"}
    assert caps["cis_profile"] == {"value": "rke2-cis-1.23", "source": "declared: profile"}  # nodes not exported → declared kept
    assert all(v["source"].split(":")[0] in {"declared", "observed", "unknown"} for v in caps.values())


@pytest.mark.parametrize("path", CONSUMER_COPIES)
def test_consumer_copies_are_valid_v1_documents(path):
    doc = json.loads((SERVICES / path).read_text(encoding="utf-8"))
    validate_inventory(doc)
    assert doc["digest"] == _digest({k: v for k, v in doc.items() if k not in {"generated_at", "digest"}})


def test_schema_version_is_fixed_and_removal_breaks_validation(repo, exports, profiles, tmp_path):
    doc = _doc(repo, exports, profiles, tmp_path)
    assert SCHEMA["properties"]["schema_version"] == {"const": "1.0"}
    for bad in ({**doc, "schema_version": "1.1"}, {k: v for k, v in doc.items() if k != "summary"}):
        with pytest.raises(jsonschema.ValidationError):
            validate_inventory(bad)
    broken = json.loads(json.dumps(doc))
    broken["candidates"][0]["capabilities"]["k8s_version"] = {"value": "unknown", "source": "observed: guess"}
    with pytest.raises(jsonschema.ValidationError):
        validate_inventory(broken)
