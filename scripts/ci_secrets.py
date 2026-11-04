"""Scan complete reachable history, with an explicit head or repository scope."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

VERSION = "8.24.3"
IMAGE = f"ghcr.io/gitleaks/gitleaks:v{VERSION}"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()


def scan(repo, scope, receipt, native=None):
    repo = repo.resolve()
    if git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("complete history required")
    head = git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    config = repo / ".gitleaks.toml"
    config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    ignore = repo / ".gitleaksignore"
    ignore_hash = hashlib.sha256(ignore.read_bytes()).hexdigest() if ignore.exists() else None
    # Include merge resolutions as well as both parents' complete histories.
    log_opts = f"--full-history --diff-merges=first-parent {head if scope == 'head' else '--all'}"
    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines() if scope == "repository" else []
    commits = int(git(repo, "rev-list", "--count", head if scope == "head" else "--all"))
    if not commits:
        raise ValueError("empty scan input")
    command = [str(native)] if native else ["docker", "run", "--rm", "-v", f"{repo}:/repo:ro", IMAGE]
    version = subprocess.check_output([*command, "version"], stderr=subprocess.PIPE, text=True).strip()
    if version != VERSION:
        raise ValueError("use the CI scanner version")
    source = str(repo) if native else "/repo"
    result = subprocess.run([
        *command, "detect", "--source", source, "--config", f"{source}/.gitleaks.toml",
        "--gitleaks-ignore-path", f"{source}/.gitleaksignore",
        "--redact", "--verbose", f"--log-opts={log_opts}",
    ], cwd=repo, check=False)
    record = {
        "scope": scope, "scanner_version": version, "config_sha256": config_hash,
        "ignore_sha256": ignore_hash,
        "checkout_sha": head, "log_opts": log_opts, "reachable_commit_count": commits,
        "fetched_refs": refs, "run_id": os.getenv("GITHUB_RUN_ID"),
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"), "exit_code": result.returncode,
        "status": "PASS" if result.returncode == 0 else "FAIL",
    }
    receipt.write_text(json.dumps(record, indent=2) + "\n")
    return result.returncode if result.returncode >= 0 else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("head", "repository"), required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--native", type=Path, help="Local CI-matching binary; default is the pinned Docker image")
    args = parser.parse_args()
    try:
        return scan(args.repo, args.scope, args.receipt, args.native)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Secret scan could not complete; check history, config and scanner version.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
