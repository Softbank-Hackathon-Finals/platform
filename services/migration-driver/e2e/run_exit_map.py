"""Isolated E2E, S5: the residual map with no person (ADR-0049, demo: map only, no block test).

usage: python run_exit_map.py <destination.json> <run-id> <inventory.json>

T45 runs infra/stacks/target-exit-map (control Pod through GitOps), then the platform makes the T27 plan and
residual map from the live snapshot. The inventory is the plan's T16 result (S1); here a file.
"""

import json
import sys
import time
from pathlib import Path

from idp import LocalIdp
from run_destination import ensure_config_repo, t10_approve

from bravo_migration.destination import Destination
from bravo_migration.host import LocalVmHost
from bravo_migration.residual_map import residual_map
from bravo_migration.vault import PlatformVault


def main():
    cfg = json.loads(Path(sys.argv[1]).read_text())
    run_id, inventory = sys.argv[2], Path(sys.argv[3])
    cfg = cfg | {"stack": "target-exit-map",
                 "params": {k: cfg["params"][k] for k in ("node_name", "arch")}}
    idp = LocalIdp(cfg["idp_state"])
    ensure_config_repo(cfg)

    def log(line):
        print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)

    destination = Destination(cfg, host=LocalVmHost(cfg["host"]["generator"], cfg["host"]["state"]),
                              vault=PlatformVault(cfg["vault_dir"]), token=lambda: idp.token(cfg["identities"]["driver"]),
                              approve=lambda binding: t10_approve(cfg, idp, binding), source_values=dict, log=log)
    run = destination.run(run_id)
    log(f"T45 {run['status']}")
    if run["status"] != "SUCCEEDED":
        sys.exit(1)
    result = residual_map(destination.kube, destination.repo_root, inventory, Path(cfg["runs_dir"]) / f"{run_id}-map")
    log(f"S5 {result['status']} {json.dumps({k: v for k, v in result.items() if k != 'status'}, ensure_ascii=False)}")
    sys.exit(0 if result["status"] == "MAPPED" else 1)


if __name__ == "__main__":
    main()
