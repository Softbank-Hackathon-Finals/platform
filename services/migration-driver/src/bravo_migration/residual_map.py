"""S5 residual map (ADR-0049, demo: map only, no block test): T27 render and map from the live snapshot.

Read only. The control Pod is already up through T45 (infra/stacks/target-exit-map). The T27 CLI runs
unchanged (services/exit-drill, stdlib only). A refused render is the result too; nothing is invented.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

NAMESPACE = "target-dev"


def _cli(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ | {"PYTHONPATH": str(repo_root / "services/exit-drill/src")}
    return subprocess.run([sys.executable, "-m", "bravo_exit_drill.cli", *args], env=env, cwd=repo_root,
                          capture_output=True, text=True, timeout=120)


def residual_map(kube, repo_root, inventory: Path, out_dir: Path, *, candidate: str = "onprem-demo") -> dict:
    repo_root, out_dir = Path(repo_root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = kube.run("get", "pods,networkpolicies", "-n", NAMESPACE, "-o", "json").stdout
    (out_dir / "snapshot.json").write_text(snapshot)
    spec = repo_root / "services/exit-drill/profiles/target-dev.json"
    rendered = _cli(repo_root, "render", "--spec", str(spec), "--inventory", str(inventory),
                    "--candidate", candidate, "--snapshot", str(out_dir / "snapshot.json"))
    (out_dir / "plan.json").write_text(rendered.stdout)
    plan = json.loads(rendered.stdout or "{}")
    if rendered.returncode != 0 or "refused" in plan:
        return {"status": "REFUSED", "refused": plan.get("refused"), "dir": str(out_dir)}
    for fmt, name in (("json", "residual-map.json"), ("markdown", "residual-map.md")):
        mapped = _cli(repo_root, "map", "--inventory", str(inventory), "--candidate", candidate,
                      "--plan", str(out_dir / "plan.json"), "--format", fmt)
        if mapped.returncode != 0:
            return {"status": "MAP_FAILED", "detail": (mapped.stderr.strip().splitlines() or [""])[0][:200]}
        (out_dir / name).write_text(mapped.stdout)
    return {"status": "MAPPED", "dir": str(out_dir), "map": "residual-map.md"}
