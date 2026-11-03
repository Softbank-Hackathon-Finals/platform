"""Select affected projects; unknown paths/history select the full suite."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

MANIFESTS = {"pyproject.toml": "python", "package.json": "node", "go.mod": "go"}
SHARED = (".github/workflows/", "scripts/ci_", "schemas/", "contracts/")
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
    selected: set[tuple[str, str]] = set()
    reasons = [full_reason] if full_reason else []
    infra = scripts = False
    for path in changed:
        if path.startswith(SHARED) or path in CONFIG:
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
            scripts |= repository_tool or (infrastructure and PurePosixPath(path).suffix.lower() == ".py")
            if owners:
                # Nested workspaces are checked with their owning parent too.
                selected.update((p["path"], p["kind"]) for p in owners)
                if PurePosixPath(path).name == "Dockerfile" or path.endswith((".Dockerfile", ".yaml", ".yml")):
                    infra = True
            elif not infrastructure and not repository_tool:
                reasons.append(f"unknown:{path}")
    full = bool(reasons)
    chosen = modules if full else [p for p in modules if (p["path"], p["kind"]) in selected]
    return {"full": full, "reasons": reasons, "changed": changed,
            "infra": full or infra, "scripts": full or scripts,
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


def plan(base: str, head: str = "HEAD", full: bool = False) -> dict:
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
    if result["projects"] and not result["full"]:
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
    args = parser.parse_args()
    result = plan(args.base, args.head, args.full)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.writelines(
                f"{key}={json.dumps(result[key], separators=(',', ':'))}\n"
                for key in ("full", "infra", "scripts", "projects", "matrix")
            )


if __name__ == "__main__":
    main()
