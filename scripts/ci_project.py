"""Run one language project's checks; same entry point locally and in Actions."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(path: Path, *command: str) -> None:
    subprocess.run(command, cwd=path, check=True)


def check(directory: str, kind: str) -> None:
    root = Path.cwd().resolve()
    path = (root / directory).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Project must be inside repository")
    if kind == "python":
        if not (path / "uv.lock").is_file():
            raise ValueError("Python projects must commit uv.lock")
        run(path, "uv", "sync", "--locked")
        run(path, "uvx", "ruff==0.17.3", "check", ".")
        run(path, "uv", "run", "--locked", "pytest")
    elif kind == "node":
        if not (path / "pnpm-lock.yaml").is_file():
            raise ValueError("Node projects must commit pnpm-lock.yaml")
        scripts = json.loads((path / "package.json").read_text()).get("scripts", {})
        required = ("lint", "test", "build")
        if not isinstance(scripts, dict) or any(
            not isinstance(scripts.get(name), str) or not scripts[name].strip()
            for name in required
        ):
            raise ValueError("Node projects must define lint, test and build scripts")
        run(path, "pnpm", "install", "--frozen-lockfile")
        for name in required:
            run(path, "pnpm", "run", name)
    elif kind == "go":
        run(path, "go", "vet", "-mod=readonly", "./...")
        run(path, "go", "test", "-mod=readonly", "./...")
    else:
        raise ValueError(f"Unsupported project kind: {kind}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("kind", choices=("python", "node", "go"))
    args = parser.parse_args()
    check(args.path, args.kind)
