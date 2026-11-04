"""Generate source digest rows and reject new ambiguous path-to-hash evidence."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LEGACY_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def revision(repo, value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("a full commit SHA is required")
    return git(repo, "rev-parse", "--verify", value + "^{commit}").decode().strip()


def source_path(value):
    return (isinstance(value, str) and value not in ("", ".") and value == str(PurePosixPath(value))
            and not PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts
            and "\\" not in value and not any(ord(char) < 32 for char in value))


def create(repo, commit, paths):
    commit = revision(repo, commit)
    if not paths or len(set(paths)) != len(paths) or not all(source_path(p) for p in paths):
        raise ValueError("distinct repository-relative source paths required")
    rows = []
    for path in sorted(paths):
        entry = git(repo, "ls-tree", commit, "--", path).decode().split(maxsplit=3)
        if len(entry) != 4 or entry[0] not in ("100644", "100755") or entry[1] != "blob":
            raise ValueError("source must be a committed regular file")
        content = git(repo, "show", f"{commit}:{path}")
        rows.append({"path": path, "sha256": hashlib.sha256(content).hexdigest()})
    return {"source_revision": commit, "source_files": rows}


def format_errors(value, previous=None, pointer=""):
    errors = []
    if isinstance(value, dict):
        # Exact, unchanged historical records remain evidence; no allowlist or rewrite.
        ambiguous = any(isinstance(v, str) and LEGACY_DIGEST.fullmatch(v)
                        and ("/" in k or bool(PurePosixPath(k).suffix)) for k, v in value.items())
        if ambiguous and value != previous:
            errors.append(pointer or "/")
        if "source_files" in value and (not isinstance(previous, dict)
                                        or value["source_files"] != previous.get("source_files")):
            rows = value["source_files"]
            valid = isinstance(rows, list) and bool(rows)
            paths = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
                    valid = False
                    continue
                if not source_path(row["path"]) or not isinstance(row["sha256"], str) or not SHA256.fullmatch(row["sha256"]):
                    valid = False
                paths.append(row["path"])
            if not valid or len(paths) != len({p for p in paths if isinstance(p, str)}):
                errors.append(pointer + "/source_files")
        for key, child in value.items():
            old = previous.get(key) if isinstance(previous, dict) else None
            errors.extend(format_errors(child, old, pointer + "/" + key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            old = previous[i] if isinstance(previous, list) and i < len(previous) else None
            errors.extend(format_errors(child, old, pointer + f"/{i}"))
    return errors


def check(repo, base):
    base = revision(repo, base)
    old_paths = set(git(repo, "ls-tree", "-r", "--name-only", "-z", base, "--", "docs/evidence").decode().split("\0"))
    paths = git(repo, "ls-files", "-z", "docs/evidence/*.json", "docs/evidence/**/*.json").decode().split("\0")
    failures = 0
    for path in filter(None, paths):
        file = repo / path
        if file.is_symlink() or not file.resolve().is_relative_to(repo.resolve()):
            raise ValueError("evidence must be a regular JSON file")
        value = json.loads(file.read_text())
        previous = json.loads(git(repo, "show", f"{base}:{path}")) if path in old_paths else None
        errors = format_errors(value, previous)
        if errors:
            # Do not print JSON keys: a malformed key can itself contain private input.
            print(f"{path}: SOURCE_HASH_FORMAT: {len(errors)} invalid structure(s)")
            failures += len(errors)
    if not failures:
        print("Source digest format passed; unchanged historical evidence preserved.")
    return int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("create")
    generate.add_argument("--revision", required=True)
    generate.add_argument("paths", nargs="+")
    validate = commands.add_parser("check")
    validate.add_argument("--base", required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            print(json.dumps(create(args.repo, args.revision, args.paths), indent=2))
            return 0
        return check(args.repo, args.base)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Source evidence validation failed: check JSON, paths and commit availability.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
