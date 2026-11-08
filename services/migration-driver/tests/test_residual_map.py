"""S5 residual map glue (ADR-0049): the T27 CLI's verdicts pass through unchanged, a refusal is a result."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "services/exit-drill/tests"))
from helpers import healthy_pods, inventory_doc, snapshot_doc  # noqa: E402

from bravo_migration.residual_map import residual_map  # noqa: E402


class Kube:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def run(self, *args):
        assert args[:2] == ("get", "pods,networkpolicies")  # read only
        return SimpleNamespace(stdout=json.dumps(self.snapshot))


def test_a_healthy_destination_gives_the_map_and_a_missing_pod_is_refused(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(inventory_doc()))
    mapped = residual_map(Kube(snapshot_doc(healthy_pods())), REPO, inventory, tmp_path / "ok")
    assert mapped["status"] == "MAPPED" and (tmp_path / "ok/residual-map.md").read_text().strip()
    without_pooler = [p for p in healthy_pods() if "pgbouncer" not in json.dumps(p["metadata"])]
    refused = residual_map(Kube(snapshot_doc(without_pooler)), REPO, inventory, tmp_path / "refused")
    assert refused["status"] == "REFUSED"
    assert {"code": "SELECTOR_MATCHES_NOTHING", "subject": "pooler"} in refused["refused"]
