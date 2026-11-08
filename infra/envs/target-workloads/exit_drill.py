#!/usr/bin/env python3
"""T27 control Pod for the S5 residual map (ADR-0049), rendered for T45 gitops_publish.

The same render `bravo-exit-drill control-pod` gives (no secrets, sleeps, ends after 4 h), with the app image
this repository pins for the node architecture, so nothing is pulled.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/exit-drill/src"))  # stdlib only; no install needed
sys.path.insert(0, str(Path(__file__).resolve().parent))

from application import pinned_image  # noqa: E402
from bravo_exit_drill.control_pod import render_control_pod  # noqa: E402
from bravo_exit_drill.spec import parse_spec  # noqa: E402

SPEC = ROOT / "services/exit-drill/profiles/target-dev.json"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["control-pod"])
    parser.add_argument("--arch", choices=["amd64", "arm64"], required=True)
    args = parser.parse_args()
    pod = render_control_pod(parse_spec(json.loads(SPEC.read_text())), pinned_image(args.arch))
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [pod]}, indent=2))
