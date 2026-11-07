"""A11: render cluster-gke steps with the T45 planner and apply them with its KubectlCluster adapter.

Used once on the real GKE cluster on 2026-11-07 instead of the full executor (no T10 approval, no Argo CD,
no config repository; see README.md). Rendering is `Planner._render` (same renderers, same pinned chart
archive and hash check) and applying is `KubectlCluster.apply` (server-side, field manager
bravo-infra-plan) or the executor's secret generator. It never prints a secret value.

    KUBECONFIG=<private kubeconfig> uv run --project services/infra-plan python \
      docs/evidence/a11-gke-base-2026-11-07/apply_steps.py apply base --repo . --charts <chart dir> \
      --private-dir <private dir> --project-id <PROJECT_ID> --kube-version v1.36.4
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path

from bravo_infra_plan.adapters import KubectlCluster
from bravo_infra_plan.executor import _random
from bravo_infra_plan.plan import HelmChartRenderer, Planner, manifest_text
from bravo_infra_plan.stack import load_stack


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["render", "apply", "secrets"])
    p.add_argument("step")
    p.add_argument("--repo", required=True)
    p.add_argument("--charts", required=True)
    p.add_argument("--private-dir", required=True)
    p.add_argument("--project-id", required=True)
    p.add_argument("--kube-version", required=True)
    p.add_argument("--side", default="target")
    p.add_argument("--out")
    a = p.parse_args()
    repo = Path(a.repo)
    stack = load_stack(repo / "infra/stacks/cluster-gke/stack.json")
    params = {"private_dir": a.private_dir, "side": a.side, "kube_version": a.kube_version,
              "project_id": a.project_id}
    stack.validate_request({"stack": "cluster-gke", "environment_id": "a11-gke", "params": params})
    step = next(s for s in stack.document["steps"] if s["id"] == a.step)

    if a.action == "secrets":  # the executor's secret_generate: create once, never overwrite
        cluster = KubectlCluster(os.environ["KUBECONFIG"])
        for secret in json.loads(json.dumps(step["secrets"]).replace("${side}", a.side)):
            if cluster.secret_exists(secret["namespace"], secret["name"]):
                print("exists", secret["namespace"], secret["name"])
                continue
            values = {k: spec["literal"] if "literal" in spec else _random(spec["random"])
                      for k, spec in secret["keys"].items()}
            cluster.create_secret(secret["namespace"], secret["name"], secret["type"], values)
            print("created", secret["namespace"], secret["name"], sorted(values))
        return

    planner = Planner(stack, repo, chart_renderer=HelmChartRenderer(a.charts))
    items = []
    for render in step["renders"]:
        rendered = planner._render(render, params)
        text = manifest_text(rendered)
        print(f"{step['path']}/{render['name']}.json sha256={hashlib.sha256(text.encode()).hexdigest()}")
        if a.out:
            out = Path(a.out) / step["path"]
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{render['name']}.json").write_text(text)
        items += rendered
    print("kinds", dict(sorted(collections.Counter(i["kind"] for i in items).items())))
    if a.action == "apply":
        KubectlCluster(os.environ["KUBECONFIG"]).apply(items)
        print("applied", len(items), "objects")


if __name__ == "__main__":
    main()
