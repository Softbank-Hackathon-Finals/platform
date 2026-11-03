"""Run one language project's checks; same entry point locally and in Actions."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(*command: str) -> None:
    subprocess.run(command, check=True)


def check(directory: str, kind: str) -> None:
    import os

    root = Path.cwd().resolve()
    path = (root / directory).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Project must be inside repository")
    os.chdir(path)
    if kind == "python":
        if not Path("uv.lock").is_file():
            raise ValueError("Python projects must commit uv.lock")
        run("uv", "sync", "--locked")
        run("uvx", "ruff==0.17.3", "check", ".")
        tests = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
        if any(Path(p).name.startswith("test_") and p.endswith(".py") or p.endswith("_test.py") for p in tests):
            run("uv", "run", "--locked", "pytest")
    elif kind == "node":
        if not Path("pnpm-lock.yaml").is_file():
            raise ValueError("Node projects must commit pnpm-lock.yaml")
        run("pnpm", "install", "--frozen-lockfile")
        scripts = json.loads(Path("package.json").read_text())["scripts"]
        for name in ("lint", "test", "build"):
            if name in scripts:
                run("pnpm", "run", name)
    elif kind == "go":
        run("go", "vet", "-mod=readonly", "./...")
        run("go", "test", "-mod=readonly", "./...")
    else:
        raise ValueError(f"Unsupported project kind: {kind}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("kind", choices=("python", "node", "go"))
    args = parser.parse_args()
    check(args.path, args.kind)
