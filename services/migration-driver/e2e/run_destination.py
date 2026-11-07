"""Isolated E2E, P stage: build the destination on a local VM with T45, approvals from the local T10.

usage: python run_destination.py <destination.json> <run-id>

The approver here is a person's stand-in (its own IdP subject, `approve_infra` only). The run itself is
the platform's: the driver subject holds `apply_infra`, claims the approval and does every operator step.
"""

import json
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bravo_platform_events.operator_identity import operator_id
from idp import ISSUER, LocalIdp
from source_fixture import Source

from bravo_migration.destination import Destination, DestinationError
from bravo_migration.host import GceRke2Host, LocalVmHost
from bravo_migration.vault import PlatformVault


def t10_approve(cfg: dict, idp: LocalIdp, binding: dict) -> str:
    body = {"approval_kind": "infra", "expires_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat(),
            "binding": binding | {"requested_by": operator_id(ISSUER, cfg["identities"]["driver"])}}
    request = urllib.request.Request(
        f"{cfg['t10']['url']}/v1/plans/{cfg['t10']['plan_id']}/approvals", method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + idp.token(cfg["identities"]["approver"]),
                 "Content-Type": "application/json", "Idempotency-Key": "approve-" + binding["infra_plan_id"]})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())["approval_id"]


def ensure_config_repo(cfg: dict) -> None:
    if "://" in cfg["config_repo"]["remote"]:
        return  # a hosted repository (GitHub) already exists; only the local bare repository is made here
    remote = Path(cfg["config_repo"]["remote"])
    if remote.exists():
        return
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    hook = remote / "hooks" / "post-update"
    hook.write_text("#!/bin/sh\nexec git update-server-info\n")
    hook.chmod(0o755)
    seed = remote.parent / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("# destination configuration (T49 isolated E2E)\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "-c", "user.name=bravo-migration", "-c", "user.email=e2e@bravo.invalid",
                    "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", str(remote), "main"], check=True)
    subprocess.run(["git", "-C", str(remote), "update-server-info"], check=True)


def main():
    cfg = json.loads(Path(sys.argv[1]).read_text())
    run_id = sys.argv[2]
    idp = LocalIdp(cfg["idp_state"])
    ensure_config_repo(cfg)
    vault = PlatformVault(cfg["vault_dir"])
    host = (GceRke2Host(cfg["host"]["node_sh"], cfg["host"]["node_json"]) if cfg["host"].get("kind") == "gce"
            else LocalVmHost(cfg["host"]["generator"], cfg["host"]["state"]))

    def source_values():
        # The source DB credential as the source holds it (Secrets Manager stand-in), read at the step:
        # the exact version with AWSCURRENT (target-secrets README step 8). Nothing is written to disk.
        value = Source(cfg).secret_value()
        if "AWSCURRENT" not in value["stages"]:
            raise DestinationError("source_secret_stage_drift", "the source secret version is not AWSCURRENT")
        return {"credentials_json": value["secret_string"], "source_version_id": value["version_id"]}

    def log(line):
        print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)

    destination = Destination(cfg, host=host, vault=vault, token=lambda: idp.token(cfg["identities"]["driver"]),
                              approve=lambda binding: t10_approve(cfg, idp, binding), source_values=source_values,
                              log=log)
    started = time.monotonic()
    try:
        run = destination.run(run_id)
    except DestinationError as e:
        log(f"stopped: {e.code}: {e}")
        sys.exit(2)
    log(f"final {run['status']} {run.get('reason', '')} in {time.monotonic() - started:.0f}s")
    sys.exit(0 if run["status"] == "SUCCEEDED" else 1)


if __name__ == "__main__":
    main()
