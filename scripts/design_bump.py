"""Prepare a design version from an explicit, locally available develop revision."""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from pathlib import Path

DOC = Path("docs/design.html")
HISTORY = Path("docs/design-history/README.md")
VERSION = r"v(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*"
HEADER = re.compile(r"(<dt>상태</dt><dd>[^<]* )(" + VERSION + r")(</dd>)")


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def version(document: str) -> str:
    matches = list(HEADER.finditer(document))
    if len(matches) != 1:
        raise ValueError("expected exactly one design version header")
    return matches[0][2]


def number(value: str) -> tuple[int, ...]:
    if not re.fullmatch(VERSION, value):
        raise ValueError("expected version such as v0.4")
    parts = tuple(map(int, value[1:].split(".")))
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def prepare(root: Path, new: str, base: str, today: str) -> tuple[str, str]:
    """Validate all inputs before any write; never switch, stage, tag or push."""
    number(new)
    branch = git(root, "branch", "--show-current").decode().strip()
    if not re.fullmatch(r"feature/[TA][0-9]+-.+", branch):
        raise ValueError("run on a feature/Tnumber-description or feature/Anumber-description branch")
    if base.startswith("-"):
        raise ValueError("invalid base revision")
    commit = git(root, "rev-parse", "--verify", base + "^{commit}").decode().strip()
    git(root, "merge-base", "--is-ancestor", commit, "origin/develop")
    git(root, "merge-base", "--is-ancestor", commit, "HEAD")
    original = git(root, "show", f"{commit}:{DOC.as_posix()}")
    old = version(original.decode("utf-8"))
    if number(new) <= number(old):
        raise ValueError("new version must be greater than the archived version")
    archive = root / HISTORY.parent / f"design-{old}.html"
    if archive.exists() or archive.is_symlink():
        raise ValueError("archive already exists; preserved copies cannot be overwritten")
    current = (root / DOC).read_bytes()
    history = (root / HISTORY).read_bytes()
    document, index = current.decode("utf-8"), history.decode("utf-8")
    if version(document) != old:
        raise ValueError("working document version differs from selected base")
    footer = re.compile(r" " + re.escape(old) + r" · \d{4}-\d{2}-\d{2} · ")
    if len(footer.findall(document)) != 1:
        raise ValueError("expected exactly one matching version footer")
    rows = list(re.finditer(
        r"^\|\s*\*\*" + re.escape(old) + r" \(현재(?: 초안)?\)\*\*\s*\|"
        r"\s*([^|\n]*?)\s*~\s*\|[^|\n]*\|\s*([^|\n]*?)\s*\|\s*([^|\n]*?)\s*\|[ \t]*$",
        index, re.MULTILINE,
    ))
    if len(rows) != 1 or re.search(r"\|\s*(?:\*\*)?" + re.escape(new) + r"[ |]", index):
        raise ValueError("history must contain one current row and no new-version collision")
    row = rows[0]
    updated = HEADER.sub(lambda m: m[1] + new + m[3], document)
    updated = footer.sub(f" {new} · {today} · ", updated)
    replacement = (
        f"| **{new} (현재 초안)** | {today} ~ | [`docs/design.html`](../design.html) | (작성: 무엇이 바뀌었나) | (관련 ADR) |\n"
        f"| {old} | {row[1]} ~ {today} | [`design-{old}.html`](design-{old}.html) | {row[2]} | {row[3]} |"
    )
    index = index[:row.start()] + replacement + index[row.end():]
    # Exclusive creation protects an archive prepared by another invocation.
    with archive.open("xb") as stream:
        stream.write(original)
    try:
        (root / DOC).write_bytes(updated.encode("utf-8"))
        (root / HISTORY).write_bytes(index.encode("utf-8"))
    except OSError:
        (root / DOC).write_bytes(current)
        (root / HISTORY).write_bytes(history)
        archive.unlink()
        raise
    return old, commit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--base", default="origin/develop", help="reviewed local develop revision")
    args = parser.parse_args()
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date().isoformat()
    try:
        old, commit = prepare(root, args.version, args.base, today)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Design preparation failed ({type(error).__name__}); inspect the local base and version files.\n")
    print(f"Prepared {old} -> {args.version} from {commit}; local files only.")
    print("Review the design body and history/ADR entries, then commit and open a PR to develop.")


if __name__ == "__main__":
    main()
