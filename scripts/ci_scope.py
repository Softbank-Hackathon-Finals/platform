"""Select affected projects; unknown paths/history select the full suite."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

MANIFESTS = {"pyproject.toml": "python", "package.json": "node", "go.mod": "go"}
SHARED = ("schemas/", "contracts/")
# The CI definition itself: prove each job still runs (one project per language), not every project.
CI_DEFINITION = (".github/workflows/ci.yml", "scripts/ci_", "scripts/tests/test_ci_")
CONFIG = {".gitleaks.toml", ".trivyignore", ".trivyignore.yaml", "ruff.toml", ".ruff.toml", "pytest.ini",
          "conftest.py", "Makefile", "uv.lock", "pnpm-lock.yaml", "go.work", "go.work.sum"}


def projects(files: list[str]) -> list[dict[str, str]]:
    found = []
    for file in files:
        path = PurePosixPath(file)
        if path.name in MANIFESTS:
            directory = str(path.parent)
            # Matrix directories are passed as env values, never executable expressions.
            if not re.fullmatch(r"[A-Za-z0-9_.\-/]+", directory) or ".." in path.parts:
                raise ValueError(f"Unsupported project path: {directory!r}")
            found.append({"path": directory, "kind": MANIFESTS[path.name]})
    return sorted(found, key=lambda p: (p["path"], p["kind"]))


# A publish workflow's image pin PR changes only these; it needs the GitOps render contract, not every check.
PIN = re.compile(r"infra/gitops/environments/[a-z0-9-]+/[a-z0-9-]+-(?:image|publication)\.json")


def documentation(path: str) -> bool:
    p = PurePosixPath(path)
    # Only prose/figures, never JSON examples, scripts or schemas stored in docs/.
    if path.startswith("docs/"):
        return p.suffix.lower() in {".md", ".html", ".svg", ".png", ".jpg", ".jpeg"}
    return p.name == "README.md" or (len(p.parts) == 1 and p.suffix == ".md") or (
        path.startswith((".agents/", ".claude/")) and p.suffix == ".md"
    )


def select(files: list[str], changed: list[str], full_reason: str = "") -> dict:
    modules = projects(files)
    if changed and not full_reason and all(PIN.fullmatch(path) for path in changed):
        return {"full": False, "reasons": [], "changed": changed, "infra": False, "scripts": True, "pins": True,
                "projects": [], "matrix": {"include": []}}
    selected: set[tuple[str, str]] = set()
    reasons = [full_reason] if full_reason else []
    infra = scripts = False
    canary = False
    for path in changed:
        if path.startswith(CI_DEFINITION):
            canary = infra = scripts = True
        elif path.startswith(".github/workflows/"):
            infra = True  # another workflow: actionlint in the infra job, no project is affected
        elif path.startswith(SHARED) or path in CONFIG:
            reasons.append(f"shared:{path}")
        elif PurePosixPath(path).name in MANIFESTS and path not in files:
            reasons.append(f"removed-manifest:{path}")
        elif documentation(path):
            continue
        else:
            owners = [p for p in modules if p["path"] == "." or path.startswith(p["path"] + "/")]
            # A language project may also live under scripts/ or infra/.
            # Directory-wide checks supplement its own tests, never replace them.
            infrastructure = path.startswith("infra/")
            repository_tool = path.startswith("scripts/")
            infra |= infrastructure
            # scripts/tests also read infra JSON (e.g. every tailnet tag must be denied the collector port),
            # so a JSON-only infra change still runs them (#912 review).
            scripts |= repository_tool or (infrastructure and PurePosixPath(path).suffix.lower() in {".py", ".json"})
            if owners:
                # Nested workspaces are checked with their owning parent too.
                selected.update((p["path"], p["kind"]) for p in owners)
                if PurePosixPath(path).name == "Dockerfile" or path.endswith((".Dockerfile", ".yaml", ".yml")):
                    infra = True
            elif not infrastructure and not repository_tool:
                reasons.append(f"unknown:{path}")
    full = bool(reasons)
    if canary and not full:
        first = {}
        for module in sorted(modules, key=lambda p: (p["kind"], p["path"])):
            if module["path"] != ".":
                first.setdefault(module["kind"], module)
        selected.update((p["path"], p["kind"]) for p in first.values())
    chosen = modules if full else [p for p in modules if (p["path"], p["kind"]) in selected]
    return {"full": full, "reasons": reasons, "changed": changed,
            "infra": full or infra, "scripts": full or scripts, "pins": False, "canary": canary and not full,
            "projects": chosen, "matrix": {"include": chosen}}


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def local_dependencies(files: list[str], read=lambda path: Path(path).read_text()) -> bool:
    """Conservatively detect local links; no second dependency-graph registry."""
    if {"go.work", "pnpm-workspace.yaml"}.intersection(files):
        return True
    for file in files:
        name = PurePosixPath(file).name
        if name not in MANIFESTS:
            continue
        text = read(file)
        if name == "pyproject.toml" and re.search(
            r"\b(?:path|workspace)\s*=|\[tool\.uv\.workspace\]|file:", text
        ):
            return True
        if name == "package.json":
            package = json.loads(text)
            if package.get("workspaces"):
                return True
            for group in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                if any(value.startswith(("file:", "link:", "workspace:", "portal:"))
                       for value in package.get(group, {}).values()):
                    return True
        if name == "go.mod" and re.search(r"=>\s*(?:[./]|[A-Za-z]:)", text):
            return True
    return False


def plan(base: str, head: str = "HEAD", full: bool = False, queue: bool = False) -> dict:
    """queue: a merge group, scoped by its whole diff from the target branch. Every PR in it already
    passed its own PR run, where local project links widen the scope; the queue does not widen again."""
    files = git("ls-files", "-z").decode().rstrip("\0").split("\0")
    if full:
        return select(files, [], "integration-or-scheduled-run")
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        return select(files, [], "missing-base")
    try:
        ancestor = git("merge-base", base, head).decode().strip()
        changed = git("diff", "--name-only", "--no-renames", "-z", ancestor, head).decode().rstrip("\0")
    except subprocess.CalledProcessError:
        return select(files, [], "unavailable-history")
    result = select(files, changed.split("\0") if changed else [])
    # A canary checks the CI machinery per language; it is not a change to those projects.
    if result["projects"] and not result["full"] and not queue and not result["canary"]:
        try:
            if local_dependencies(files):
                return select(files, result["changed"], "local-project-dependencies")
        except (OSError, ValueError, TypeError, AttributeError):
            return select(files, result["changed"], "unreadable-dependencies")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--queue", action="store_true")
    args = parser.parse_args()
    result = plan(args.base, args.head, args.full, args.queue)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.writelines(
                f"{key}={json.dumps(result[key], separators=(',', ':'))}\n"
                for key in ("full", "infra", "scripts", "pins", "projects", "matrix")
            )


if __name__ == "__main__":
    main()
