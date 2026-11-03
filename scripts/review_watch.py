"""Read-only review arrival notifications for the signed-in platform PR author."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from review_packet import GitHub, PacketError

REPO = "Softbank-Hackathon-Finals/platform"
ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc  # noqa: UP017 - standalone macOS Python 3.9 compatibility.
NOTIFY_SCRIPT = "on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run"
LOGIN = re.compile(r"[A-Za-z0-9_-]+(?:\[bot\])?")


class ReviewGitHub(GitHub):
    def __init__(self):
        super().__init__(REPO)

    def api(self, path: str) -> object:
        return self.call("api", "--hostname", "github.com", "--method", "GET", f"repos/{REPO}/{path}")

    def viewer(self) -> str:
        data = self.call("api", "--hostname", "github.com", "--method", "GET", "user")
        login = data.get("login") if isinstance(data, dict) else None
        if not isinstance(login, str) or not LOGIN.fullmatch(login):
            raise PacketError("Authenticated GitHub identity unavailable")
        return login


def timestamp(value: object) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone")
        return parsed.astimezone(UTC)
    except ValueError as exc:
        raise PacketError("Invalid timestamp; cursor unchanged") from exc


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def own_pr(row: dict, viewer: str) -> int | None:
    user = row.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise PacketError("PR author unavailable; cursor unchanged")
    if user["login"].lower() != viewer.lower():
        return None
    number = row.get("number")
    if type(number) is not int or number < 1 or row.get("state") not in {"open", "closed"}:
        raise PacketError("Incomplete PR discovery; cursor unchanged")
    return number


def event(row: dict, number: int, kind: str) -> dict | None:
    state = row.get("state") if kind == "review" else "INLINE_COMMENT"
    if state == "PENDING":
        return None
    if kind == "review" and state not in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}:
        raise PacketError("Unknown review state; cursor unchanged")
    identifier = row.get("id")
    if type(identifier) is not int or identifier < 1:
        raise PacketError("Missing review identifier; cursor unchanged")
    user = row.get("user")
    if user is not None and not isinstance(user, dict):
        raise PacketError("Review author unavailable; cursor unchanged")
    author = (user or {}).get("login", "deleted-user")
    if not isinstance(author, str) or not LOGIN.fullmatch(author):
        raise PacketError("Invalid review author; cursor unchanged")
    when = iso(timestamp(row.get("submitted_at" if kind == "review" else "updated_at")))
    commit = row.get("commit_id")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PacketError("Missing review revision; cursor unchanged")
    anchor = f"pullrequestreview-{identifier}" if kind == "review" else f"discussion_r{identifier}"
    # Never retain comment bodies, diff hunks, filenames, email, or API user IDs.
    return {"kind": kind, "id": identifier, "pr": number, "state": state, "author": author,
            "at": when, "commit": commit, "url": f"https://github.com/{REPO}/pull/{number}#{anchor}"}


def collect(client: ReviewGitHub, viewer: str, previous: dict | None, now: dt.datetime) -> tuple[dict, dict]:
    """Collect only discovery/review streams, including PRs merged between polls."""
    cutoff = timestamp(previous["last_success"]) - dt.timedelta(minutes=10) if previous else now - dt.timedelta(days=30)
    open_numbers = set()
    targets = set()
    for row in client.pages("pulls?state=open&sort=updated&direction=desc"):
        number = own_pr(row, viewer)
        if number is not None:
            open_numbers.add(number)
            targets.add(number)
    query = urlencode({"creator": viewer, "state": "all", "since": iso(cutoff), "sort": "updated", "direction": "desc"})
    for row in client.pages(f"issues?{query}"):
        if "pull_request" not in row:
            continue
        number = own_pr(row, viewer)
        if number is not None:
            targets.add(number)
    # The discovery APIs can lag. Re-read formerly open PRs once after they disappear.
    for number in set(previous["open_prs"] if previous else []) - targets:
        row = client.api(f"pulls/{number}")
        if not isinstance(row, dict) or own_pr(row, viewer) != number:
            raise PacketError("Previously watched PR ownership unavailable; cursor unchanged")
        targets.add(number)
        if row["state"] == "open":
            open_numbers.add(number)
    seen = dict(previous["seen"] if previous else {})
    changes = []
    for number in sorted(targets):
        for endpoint, kind in (("reviews", "review"), ("comments", "inline_comment")):
            for row in client.pages(f"pulls/{number}/{endpoint}"):
                item = event(row, number, kind)
                if item is None:
                    continue
                key = f"{number}:{kind}:{item['id']}"
                fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                if seen.get(key) != fingerprint:
                    changes.append({**item, "arrival": "EXISTING" if previous is None else "UPDATED" if key in seen else "NEW"})
                seen[key] = fingerprint
    changes.sort(key=lambda item: (item["at"], item["pr"], item["id"]))
    cursor = {"version": 1, "repository": REPO, "viewer": viewer, "last_success": iso(now),
              "open_prs": sorted(open_numbers), "seen": seen}
    report = {"status": "BASELINE" if previous is None else "OK", "repository": REPO,
              "checked_at": iso(now), "discovery_since": iso(cutoff), "checked_prs": sorted(targets),
              "events": changes, "meaning": "Arrival only; not review assessment, approval eligibility, or merge permission."}
    return report, cursor


def private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise PacketError("Cursor and lock must be owned regular files with mode 0600")


def state_path(value: str | None, viewer: str) -> Path:
    path = Path(value).expanduser() if value else Path.home() / ".local/state/bravo-review-watch" / f"{viewer}.json"
    if path.is_symlink():
        raise PacketError("Cursor symlinks are not supported")
    path = path.absolute()
    resolved = path.resolve()
    if resolved == ROOT or ROOT in resolved.parents or ".git" in resolved.parts:
        raise PacketError("Cursor must be outside Git repositories")
    ancestor = path.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    result = subprocess.run(["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode == 0:
        raise PacketError("Cursor must be outside Git repositories")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PacketError("Use a dedicated cursor directory owned by you with mode 0700")
    return path


@contextlib.contextmanager
def cursor_lock(path: Path):
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_file(lock)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PacketError("Another watcher is using this cursor") from exc
        yield
    finally:
        os.close(fd)


def read_cursor(path: Path, viewer: str) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    private_file(path)
    try:
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or data.get("version") != 1 or data.get("repository") != REPO
                or data.get("viewer") != viewer or not isinstance(data.get("open_prs"), list)
                or any(type(n) is not int or n < 1 for n in data["open_prs"])
                or not isinstance(data.get("seen"), dict)
                or any(not re.fullmatch(r"\d+:(?:review|inline_comment):\d+", key)
                       or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for key, value in data["seen"].items())):
            raise ValueError("schema")
        timestamp(data.get("last_success"))
        return data
    except (ValueError, TypeError) as exc:
        raise PacketError("Cursor invalid or belongs to another identity; use a separate cursor") from exc


def save_cursor(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".review-watch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def notify(message: str) -> None:
    result = subprocess.run(["osascript", "-e", NOTIFY_SCRIPT, message, "Platform PR reviews"],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        raise PacketError("macOS notification unavailable; terminal output still contains arrivals")


def emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return
    print(f"{report['status']}: {len(report.get('events', []))} review event(s); "
          f"{len(report.get('checked_prs', []))} own PR(s) checked", flush=True)
    if report["status"] == "BASELINE":
        print("Existing reviews recorded; desktop notifications start on subsequent polls.", flush=True)
    for item in report.get("events", []):
        print(f"{item['arrival']} #{item['pr']} {item['state']} by {item['author']} at {item['at']}\n  {item['url']}", flush=True)
    if report.get("error"):
        print(report["error"], flush=True)


def install_alias() -> None:
    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=10, check=False)
    origin = git("remote", "get-url", "origin")
    if origin.returncode or origin.stdout.strip().removesuffix(".git") not in {
        f"https://github.com/{REPO}", f"git@github.com:{REPO}", f"ssh://git@github.com/{REPO}"
    }:
        raise PacketError("Alias setup requires the platform origin")
    # Linked worktrees share local Git config. Bind the verified checkout so the
    # alias also works from branches that do not contain this script yet.
    alias = "!python3 " + shlex.quote(str(ROOT / "scripts/review_watch.py"))
    existing = git("config", "--local", "--get-all", "alias.review-watch")
    if existing.returncode not in (0, 1) or (existing.returncode == 0 and existing.stdout.strip() != alias):
        raise PacketError("Existing local alias differs; left unchanged")
    if git("config", "--local", "alias.review-watch", alias).returncode:
        raise PacketError("Could not install local Git alias")


def parser() -> argparse.ArgumentParser:
    args = argparse.ArgumentParser(description=__doc__)
    mode = args.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one poll (default)")
    mode.add_argument("--watch", action="store_true", help="foreground polling; Ctrl-C to stop")
    args.add_argument("--interval", type=int, default=60, help="seconds after each poll; minimum 60")
    args.add_argument("--json", action="store_true", help="one JSON record per poll")
    args.add_argument("--notify", action="store_true", help="opt-in macOS notification for new arrivals")
    args.add_argument("--state-file", help="private cursor outside all Git repositories")
    args.add_argument("--install-git-alias", action="store_true", help="install repository-local alias, then exit")
    return args


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.interval < 60:
        arguments.error("--interval must be at least 60 seconds")
    if args.notify and sys.platform != "darwin":
        arguments.error("--notify requires macOS")
    try:
        if args.install_git_alias:
            install_alias()
            emit({"status": "ALIAS_INSTALLED"}, args.json)
            return 0
        client = ReviewGitHub()
        viewer = client.viewer()
        path = state_path(args.state_file, viewer)
        with cursor_lock(path):
            previous = read_cursor(path, viewer)
            while True:
                try:
                    report, cursor = collect(client, viewer, previous, dt.datetime.now(UTC))
                    emit(report, args.json)
                    if args.notify and report["status"] != "BASELINE" and report["events"]:
                        try:
                            notify(f"{len(report['events'])} new or changed reviews/comments. See terminal output for PR links.")
                        except (PacketError, OSError, subprocess.TimeoutExpired):
                            print("Notification unavailable; events were printed to the terminal.", file=sys.stderr)
                    save_cursor(path, cursor)
                    previous = cursor
                    result = 0
                except (PacketError, OSError, subprocess.TimeoutExpired):
                    emit({"status": "UNKNOWN", "error": "Poll failed; cursor unchanged. Check GitHub access and private state permissions."}, args.json)
                    result = 1
                if not args.watch:
                    return result
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (PacketError, OSError, subprocess.TimeoutExpired):
        emit({"status": "UNKNOWN", "error": "Setup failed; cursor unchanged. Check authentication, cursor ownership, and local alias."}, args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
