"""Compile an existing reviewed T15 fixture input into one nonsecret environment record."""

import argparse
import hashlib
import json
from pathlib import Path

from .recipes import IMAGES, REPO, SOURCE_PATHS, load_module, profile


def render(config, architecture, name, namespace):
    harness = load_module(
        "credential_harness",
        REPO / "services/rehearsal-runner/testing/rke2_credential_flow.py",
    )
    diff, _ = harness.proposal_diff(config)
    value = profile(
        {
            "schema": "bravo.team-runtime/v1",
            "cluster_uid": config["cluster_uid"],
            "architecture": architecture,
            "images": {key: config[key] for key in IMAGES},
            "source_sha": config["source_sha"],
            "sources": {
                p: (Path(config["source_dir"]) / p).read_text() for p in SOURCE_PATHS
            },
            "proposal": json.loads(Path(config["proposal"]).read_text()),
            "diff_hash": "sha256:" + hashlib.sha256(diff).hexdigest(),
            "isolation_profile": Path(config["profile"]).read_text(),
        }
    )
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "component": "team-diagnostics",
                "app.kubernetes.io/managed-by": "bravo-team-runtime",
            },
        },
        "data": {"environment.json": json.dumps(value, sort_keys=True)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--architecture", choices=("arm64", "amd64"), required=True)
    parser.add_argument("--name", default="bravo-self-service-runtime")
    parser.add_argument("--namespace", default="bravo-team-access")
    args = parser.parse_args()
    print(
        json.dumps(
            render(
                json.loads(args.config.read_text()),
                args.architecture,
                args.name,
                args.namespace,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
