"""`terraform` steps (ADR-0033 3.2, second slice): the existing infra/envs stacks, in order, wired.

What the approval binds (planner) and what the executor re-checks before it plans:
- the code: every file of the stack directory and of each local module it sources (`../../modules/...`),
  plus `.terraform.lock.hcl`, which is used read-only;
- the private inputs by digest only: the backend file and the stack's tfvars file live outside the
  repository (account IDs and resource IDs never enter Git or a plan document);
- the wiring: which variable comes from which upstream output path.

At apply time the executor plans to a file, refuses a plan that deletes or replaces anything, and
applies exactly that plan file. Upstream outputs are read from state when needed and passed in memory;
they are not stored in the run record (several are marked sensitive even though they are IDs).
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .stack import PLACEHOLDER, StackError, canonical, sha256_text

MODULE_SOURCE = re.compile(r'^\s*source\s*=\s*"(\.\.?/[^"]+)"', re.M)
CODE_SUFFIXES = (".tf", ".tf.json", ".hcl")
TIMEOUT = 1800


def substitute(value, params):
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: params[m.group(1)], value)
    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}
    return value


def _tree(directory: Path):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.iterdir()) if p.is_file() and p.name.endswith(CODE_SUFFIXES)}


def code_digest(repo_root: Path, directory: str):
    """The stack directory and, transitively, the local modules it sources."""
    root = (repo_root / directory).resolve()
    if repo_root not in root.parents or not root.is_dir():
        raise StackError("terraform_dir_unavailable")
    seen, pending, trees = set(), [root], {}
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        trees[str(current.relative_to(repo_root))] = _tree(current)
        for file in current.glob("*.tf"):
            for source in MODULE_SOURCE.findall(file.read_text()):
                module = (current / source).resolve()
                if repo_root not in module.parents or not module.is_dir():
                    raise StackError("terraform_module_outside_repository")
                pending.append(module)
    return sha256_text(canonical(trees))


def private_file_digest(repo_root: Path, path: str | None):
    """Digest of an operator file that must live outside the repository (or None when not given)."""
    if path is None:
        return None
    file = Path(path)
    if not file.is_absolute() or repo_root in file.resolve().parents or not file.is_file():
        raise StackError("terraform_private_input_unavailable")
    return hashlib.sha256(file.read_bytes()).hexdigest()


def inputs_digest(repo_root: Path, step, params):
    """Everything the approval binds for one terraform step; recomputed before every plan."""
    resolved = substitute({k: step[k] for k in ("backend", "vars") if k in step}, params)
    var_file = substitute(step.get("var_file"), params)
    return {"code": code_digest(repo_root, step["dir"]),
            "backend_file": private_file_digest(repo_root, resolved["backend"]["file"]),
            "var_file": private_file_digest(repo_root, var_file),
            "wiring": sha256_text(canonical({"backend_config": resolved["backend"].get("config", {}),
                                             "vars": resolved.get("vars", {})}))}


def _walk(value, path):
    for part in path.split(".") if path else []:
        if not isinstance(value, dict) or part not in value:
            raise StackError("terraform_output_missing")
        value = value[part]
    if value is None:
        raise StackError("terraform_output_missing")
    return value


def output_path(outputs, reference):
    """One value (`path`) or a list of values (`paths`) from an upstream output."""
    value = outputs.get(reference["output"], {}).get("value")
    if "paths" in reference:
        return [_walk(value, path) for path in reference["paths"]]
    return _walk(value, reference.get("path"))


class TerraformCli:
    """terraform with a per-step data directory, never writing into the repository."""

    def __init__(self, *, terraform="terraform", extra_env=None):
        self.terraform = terraform
        allowed = ("PATH", "HOME", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE",
                   "AWS_SHARED_CREDENTIALS_FILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
        self.base_env = {k: os.environ[k] for k in allowed if k in os.environ} | (extra_env or {})
        self.base_env |= {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"}

    def _run(self, directory, data_dir, *args, check=True):
        env = self.base_env | {"TF_DATA_DIR": str(data_dir)}
        result = subprocess.run([self.terraform, f"-chdir={directory}", *args], env=env, capture_output=True,
                                text=True, timeout=TIMEOUT, check=False)
        if check and result.returncode != 0:
            raise RuntimeError(f"terraform {args[0]} failed")
        return result

    def init(self, directory, data_dir, backend_file, backend_config):
        args = ["init", "-input=false", "-reconfigure", "-lockfile=readonly", f"-backend-config={backend_file}"]
        args += [f"-backend-config={k}={v}" for k, v in sorted(backend_config.items())]
        self._run(directory, data_dir, *args)

    def outputs(self, directory, data_dir):
        return json.loads(self._run(directory, data_dir, "output", "-json").stdout or "{}")

    def plan(self, directory, data_dir, plan_file, var_files, *, lock=True):
        args = ["plan", "-input=false", "-detailed-exitcode", f"-out={plan_file}"]
        args += ["-lock-timeout=120s"] if lock else ["-lock=false"]  # read-only verification takes no lock
        args += [f"-var-file={f}" for f in var_files]
        result = self._run(directory, data_dir, *args, check=False)
        if result.returncode not in (0, 2):
            raise RuntimeError("terraform plan failed")
        return result.returncode == 2

    def show(self, directory, data_dir, plan_file):
        return json.loads(self._run(directory, data_dir, "show", "-json", str(plan_file)).stdout)

    def apply(self, directory, data_dir, plan_file):
        self._run(directory, data_dir, "apply", "-input=false", "-lock-timeout=120s", str(plan_file))


def change_summary(plan_json):
    """Counts per action and whether anything is deleted or replaced (a replace is a delete)."""
    counts, destructive = {}, []
    for change in plan_json.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if actions == ["no-op"]:
            continue
        key = "replace" if "delete" in actions and "create" in actions else "+".join(actions)
        counts[key] = counts.get(key, 0) + 1
        if "delete" in actions:
            destructive.append(change.get("address", "?"))
    return counts, destructive


class Scratch:
    """A private directory per step run: data dir, generated tfvars and the plan file."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory(prefix="bravo-tf-")
        os.chmod(self._dir.name, 0o700)
        return Path(self._dir.name)

    def __exit__(self, *exc):
        self._dir.cleanup()


def verify(stack, repo_root, params, *, only=None, cli=None):
    """Plan every terraform step in order against the real state, never apply and never lock.

    For a stack that is already applied, every step should report no change: that is the check that
    the wiring reproduces what operators typed by hand. Upstream outputs are read from state.
    """
    cli = cli or TerraformCli()
    steps = {s["id"]: s for s in stack.document["steps"] if s["kind"] == "terraform"}
    results = []
    for level in stack.levels:
        for step_id in level:
            if step_id not in steps or (only and step_id not in only):
                continue
            step = steps[step_id]
            resolved = substitute({k: step[k] for k in ("backend", "var_file") if k in step}, params)
            directory = Path(repo_root) / step["dir"]
            try:
                inputs_digest(Path(repo_root), step, params)  # private files exist and live outside Git
                values = {}
                for name, value in step.get("vars", {}).items():
                    if isinstance(value, dict):
                        upstream = steps[value["from"]]
                        backend = substitute(upstream["backend"], params)
                        with Scratch() as scratch:
                            cli.init(Path(repo_root) / upstream["dir"], scratch / "data", backend["file"],
                                     backend.get("config", {}))
                            values[name] = output_path(cli.outputs(Path(repo_root) / upstream["dir"],
                                                                   scratch / "data"), value)
                    else:
                        values[name] = substitute(value, params)
                with Scratch() as scratch:
                    cli.init(directory, scratch / "data", resolved["backend"]["file"],
                             resolved["backend"].get("config", {}))
                    wired = scratch / "wired.tfvars.json"
                    wired.write_text(json.dumps(values))
                    var_files = ([resolved["var_file"]] if resolved.get("var_file") else []) + [str(wired)]
                    changed = cli.plan(directory, scratch / "data", scratch / "plan.bin", var_files, lock=False)
                    counts, destructive = (change_summary(cli.show(directory, scratch / "data", scratch / "plan.bin"))
                                           if changed else ({}, []))
                results.append({"step": step_id, "status": "CHANGES" if changed else "NO_CHANGES",
                                "changes": counts, "destructive": len(destructive)})
            except (StackError, RuntimeError) as error:
                results.append({"step": step_id, "status": "ERROR", "reason": str(error)})
    return results
