"""Render a stack into a plan and its `plan_digest` (ADR-0033 3.3).

The planner only reads: it runs repository renderers and `helm template`, never `kubectl`, and
never writes to a cluster or a Git remote. A plan holds digests and the rendered manifests; the
manifests come from renderers that never emit credential values, and secret steps carry only
Secret and key names. The same request, stack, revision, target and base commit give the same
`plan_digest`; the executor recomputes it and refuses a run when it differs from the approval.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .stack import PLACEHOLDER, Stack, StackError, canonical, sha256_text
from .terraform import Scratch, inputs_digest, outputs_reader, plan_step

RENDER_TIMEOUT = 120


def _substitute(args, params):
    return [PLACEHOLDER.sub(lambda m: params[m.group(1)], arg) for arg in args]


def _items(value):
    if isinstance(value, dict) and value.get("kind") == "List":
        value = value.get("items")
    elif isinstance(value, dict) and "apiVersion" in value and "kind" in value:
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise StackError("render_not_manifest_list")
    return value


def _select(items, select):
    if not select:
        return items
    kinds, excluded = select.get("kinds"), select.get("exclude_kinds", [])
    return [i for i in items if (kinds is None or i.get("kind") in kinds) and i.get("kind") not in excluded]


def manifest_text(items) -> str:
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": items},
                      sort_keys=True, indent=2, ensure_ascii=False) + "\n"


class RepositoryRenderer:
    """Runs a repository renderer with fixed arguments. No shell and no inherited secrets."""

    def __init__(self, repo_root, *, python=sys.executable):
        self.repo_root, self.python = Path(repo_root).resolve(), python

    def run(self, script, args, stdin=None):
        path = (self.repo_root / script).resolve()
        if self.repo_root not in path.parents or not path.is_file():
            raise StackError("renderer_unavailable")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        result = subprocess.run([self.python, str(path), *args], cwd=self.repo_root, input=stdin,
                                capture_output=True, text=True, timeout=RENDER_TIMEOUT, env=env, check=False)
        if result.returncode != 0:
            raise StackError("renderer_failed")
        return result.stdout


class HelmChartRenderer:
    """`helm template` over a pinned chart archive. The archive hash is checked before use."""

    def __init__(self, charts_dir, *, helm="helm"):
        self.charts_dir, self.helm = Path(charts_dir), helm

    def template(self, chart, *, release, namespace, values_path, include_crds, kube_version):
        archive = self.charts_dir / chart["file"]
        if not archive.is_file():
            raise StackError("chart_unavailable")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != chart["sha256"]:
            raise StackError("chart_hash_mismatch")
        command = [self.helm, "template", release, str(archive), "--namespace", namespace,
                   "--values", str(values_path), "--skip-tests", "--kube-version", kube_version]
        if include_crds:
            command.append("--include-crds")
        result = subprocess.run(command, capture_output=True, text=True, timeout=RENDER_TIMEOUT, check=False)
        if result.returncode != 0:
            raise StackError("chart_render_failed")
        return [o for o in yaml.safe_load_all(result.stdout) if o]


@dataclass
class Plan:
    document: dict
    files: dict = field(default_factory=dict)  # step id -> {relative file: manifest text}

    @property
    def plan_digest(self) -> str:
        return self.document["plan_digest"]


class Planner:
    def __init__(self, stack: Stack, repo_root, *, chart_renderer=None, renderer=None,
                 kube_version_param="kube_version", terraform=None):
        self.stack, self.repo_root = stack, Path(repo_root).resolve()
        self.renderer = renderer or RepositoryRenderer(self.repo_root)
        self.charts = chart_renderer
        self.kube_version_param = kube_version_param
        self.terraform = terraform  # a TerraformCli to bind each terraform step's change set

    def plan(self, request, *, stack_revision, target, base_commit) -> Plan:
        request = self.stack.validate_request(request)
        # The identity of what this plan changes: a cluster and node, or an AWS account.
        if not isinstance(target, dict) or not target or not all(
                isinstance(k, str) and isinstance(v, str) and v for k, v in target.items()):
            raise StackError("target_identity_required")
        for value in (stack_revision, base_commit):
            if not isinstance(value, str) or len(value) != 40 or not all(c in "0123456789abcdef" for c in value):
                raise StackError("git_revision_required")
        params = request["params"]
        steps, files, tree = [], {}, {}
        for step in self.stack.document["steps"]:
            entry = {"id": step["id"], "kind": step["kind"], "owner": step["owner"], "needs": step.get("needs", [])}
            if step["kind"] in ("gitops_publish", "render_apply"):
                rendered = self._render_step(step, params)
                files[step["id"]] = rendered
                entry["files"] = {name: sha256_text(text) for name, text in sorted(rendered.items())}
                entry["digest"] = sha256_text(canonical(entry["files"]))
                if step["kind"] == "gitops_publish":
                    tree.update(entry["files"])
            elif step["kind"] == "terraform":
                entry["inputs"] = inputs_digest(self.repo_root, step, params)
                entry["change_set"], entry["changes"] = self._terraform_change_set(step, params)
                entry["digest"] = sha256_text(canonical({"step": step, "inputs": entry["inputs"],
                                                         "change_set": entry["change_set"]}))
            elif step["kind"] == "operator_action" and "desired" in step:
                # The values a person must use come from T44's desired state, bound into the plan.
                entry["desired"] = self._desired(step["desired"])
                entry["digest"] = sha256_text(canonical({"step": step, "desired": entry["desired"]}))
            else:
                entry["digest"] = sha256_text(canonical(step))
            steps.append(entry)
        document = {
            "schema_version": 1, "stack": self.stack.name, "stack_digest": self.stack.digest,
            "stack_revision": stack_revision, "request": request, "target": dict(target),
            "base_commit": base_commit, "levels": [list(level) for level in self.stack.levels],
            "steps": steps, "tree_digest": sha256_text(canonical(tree)),
        }
        document["plan_digest"] = sha256_text(canonical(document))
        return Plan(document, files)

    def _render_step(self, step, params):
        prefix = step["path"] + "/" if step["kind"] == "gitops_publish" else ""
        return {f"{prefix}{render['name']}.json": manifest_text(_select(self._render(render, params),
                                                                         render.get("select")))
                for render in step["renders"]}

    def _render(self, render, params):
        if isinstance(render.get("namespace"), dict):  # a namespace T45 owns that no renderer emits (ADR-0033, T44 #632)
            spec = render["namespace"]
            return [{"apiVersion": "v1", "kind": "Namespace", "metadata": {
                "name": spec["name"], "labels": {"app.kubernetes.io/managed-by": "bravo-infra-plan"} | spec["labels"]}}]
        if "run" in render:
            return _items(json.loads(self.renderer.run(render["run"], _substitute(render["args"], params))))
        if self.charts is None:
            raise StackError("chart_renderer_required")
        chart = self.stack.document["charts"][render["chart"]]
        with tempfile.TemporaryDirectory() as scratch:
            if "values" in render:
                values = Path(scratch) / "values.json"
                values.write_text(self.renderer.run(render["values"]["run"],
                                                    _substitute(render["values"]["args"], params)))
            else:
                values = self._repo_file(render["values_file"])
            items = self.charts.template(chart, release=render["release"], namespace=render["namespace"],
                                         values_path=values, include_crds=render.get("include_crds", False),
                                         kube_version=params[self.kube_version_param])
        if render.get("create_namespace"):  # `helm template` never emits the release namespace
            items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {
                "name": render["namespace"], "labels": {"app.kubernetes.io/managed-by": "bravo-infra-plan"}}}] + items
        if "post" in render:
            post = render["post"]
            items = _items(json.loads(self.renderer.run(post["run"], _substitute(post["args"], params),
                                                        stdin=json.dumps(items))))
        return items

    def _terraform_change_set(self, step, params):
        """What terraform would change now (addresses and actions, no values), bound into the approval.

        UNPLANNED: no terraform client was given (offline). PENDING_UPSTREAM: an upstream stack is not
        applied yet, so its outputs do not exist; the executor stops there for a new approval."""
        if self.terraform is None:
            return "UNPLANNED", {}
        steps = {s["id"]: s for s in self.stack.document["steps"] if s["kind"] == "terraform"}
        read = outputs_reader(self.terraform, self.repo_root, steps, params)
        try:
            with Scratch() as scratch:
                planned = plan_step(self.terraform, self.repo_root, step, params, read, scratch, lock=False)
        except StackError as error:
            if str(error) == "terraform_output_missing":
                return "PENDING_UPSTREAM", {}
            raise
        return planned["change_set"], planned["changes"]

    def _desired(self, spec):
        state = json.loads(self._repo_file(spec["file"]).read_text())
        modules = [m for m in state.get("modules", []) if m.get("id") == spec["module"]]
        if len(modules) != 1:
            raise StackError("desired_module_unavailable")
        params = modules[0].get("params", {})
        return {name: params[name] for name in spec["params"] if name in params}

    def _repo_file(self, relative):
        path = (self.repo_root / relative).resolve()
        if self.repo_root not in path.parents or not path.is_file():
            raise StackError("repository_file_unavailable")
        return path
