"""Fetch the catalog's approved public source without application credentials."""

import json
import os
import re
import subprocess
from pathlib import Path


def approved_release(catalog):
    releases = catalog.get("releases")
    if not isinstance(releases, list) or len(releases) != 1:
        raise ValueError("one_approved_release_required")
    release = releases[0]
    repo, revision = release["repo_url"], release["revision"]
    if (not isinstance(repo, str) or re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", repo
    ) is None or not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{40}", revision) is None):
        raise ValueError("approved_https_revision_required")
    if release.get("checkout") != "/checkout/repo":
        raise ValueError("approved_checkout_path_required")
    return repo, revision


def fetch(catalog, destination=Path("/checkout/repo")):
    repo, revision = approved_release(catalog)
    env = {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}

    def git(*args, check=True):
        return subprocess.run(
            ["git", "-c", "http.followRedirects=false", "-c", "protocol.file.allow=never",
             "-c", "protocol.ext.allow=never", *args],
            env=env, capture_output=True, text=True, timeout=60, check=check,
        )

    if destination.exists():
        # Reuse only a checkout whose origin and fetched commit both match.
        if git("-C", str(destination), "remote", "get-url", "origin").stdout.strip() != repo:
            raise ValueError("existing_checkout_mismatch")
        current = git("-C", str(destination), "rev-parse", "--verify", "FETCH_HEAD", check=False)
        if current.returncode == 0:
            if current.stdout.strip() != revision:
                raise ValueError("existing_checkout_mismatch")
            return
        # A failed fetch can leave .git without FETCH_HEAD. Retrying the same
        # read-only fetch is safe; never replace a different existing checkout.
    else:
        git("init", str(destination))
        git("-C", str(destination), "remote", "add", "origin", repo)
    git("-C", str(destination), "fetch", "--depth=1", "origin", revision)
    if git("-C", str(destination), "rev-parse", "FETCH_HEAD").stdout.strip() != revision:
        raise ValueError("fetched_revision_mismatch")


def main():
    try:
        fetch(json.loads(Path("/registration/catalog.json").read_text()))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise SystemExit("registration_checkout_unavailable") from None
    print("registration_checkout_ready")


if __name__ == "__main__":
    main()
