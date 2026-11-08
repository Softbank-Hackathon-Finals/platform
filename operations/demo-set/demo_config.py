"""Shared settings for the demo set: one local JSON file, never committed (see demo.example.json).

BRAVO_DEMO_CONFIG points at it (default ~/.bravo-demo/demo.json). Every cluster call checks the
cluster UID first, so a wrong kubeconfig stops before any change.
"""

import json
import os
import subprocess
from pathlib import Path

CONFIG = Path(os.environ.get("BRAVO_DEMO_CONFIG", Path.home() / ".bravo-demo" / "demo.json"))
SIDES = ("platform", "source", "target")


def load():
    cfg = json.loads(CONFIG.read_text())
    cfg["workdir"] = Path(os.path.expanduser(cfg["workdir"]))
    cfg["workdir"].mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg["repo"] = Path(os.path.expanduser(cfg.get("repo") or Path(__file__).resolve().parents[2]))
    return cfg


def kubeconfig(cfg, side):
    return os.path.expanduser(cfg["clusters"][side]["kubeconfig"])


def kubectl(cfg, side, *args, input=None, timeout=60):
    return subprocess.run(["kubectl", "--kubeconfig", kubeconfig(cfg, side), *args], input=input,
                          capture_output=True, text=True, check=True, timeout=timeout).stdout


def check(cfg, side):
    """Stop unless the kubeconfig reaches the registered cluster (kube-system UID)."""
    uid = kubectl(cfg, side, "get", "ns", "kube-system", "-o", "jsonpath={.metadata.uid}")
    if uid != cfg["clusters"][side]["uid"]:
        raise SystemExit(f"{side}: cluster UID {uid!r} is not the registered one")
    return uid


def plan_dir(cfg, plan_id):
    path = cfg["workdir"] / plan_id
    path.mkdir(mode=0o700, exist_ok=True)
    return path
