"""Check public artifacts; preserve private originals before explicit sanitization."""

import argparse
import collections
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "EMAIL": re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "HOME": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+"),
    "AWS_ACCOUNT": re.compile(r"(?<![\w.])\d{12}(?![\w.])"),
    "AWS_RESOURCE": re.compile(r"\b(?:vpc|subnet|sg|eni|rtb|pcx|igw|nat|eipalloc|vol|snap|ami|i|key)-[0-9a-f]{8,17}\b"),
    "AWS_ARN": re.compile(r"arn:aws(?:-cn|-us-gov)?:[^\s\"'<>]+"),
    "ENDPOINT": re.compile(r"\b[A-Za-z0-9.-]+\.(?:compute|elb|rds)\.amazonaws\.com\b"),
}
IP = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w./])")
EXAMPLE_ACCOUNTS = {"123456789012", "111122223333", "000000000000", "111111111111", "222222222222"}
NAME_FIELDS = {"groupname", "keyname", "keypairname", "loggroupname", "bucketname",
               "privatednsname", "publicdnsname", "hostname", "endpoint"}
ACCOUNT_FIELDS = {"account", "accountid", "awsaccountid", "sourceaccountid", "targetaccountid"}
PRESERVED_FIELDS = {"decision", "verdict", "status", "outcome", "conclusion", "passed", "failed",
                    "measurements", "metrics", "counts", "timings", "timestamp", "time", "date"}
DIGEST = re.compile(r"(?:sha(?:1|224|256|384|512):)?[0-9a-fA-F]{32,128}\Z")


def resource_name_field(field, parent):
    return (field.lower().replace("_", "") in NAME_FIELDS or field == "log_groups"
            or (field == "Value" and isinstance(parent, dict) and parent.get("Key") == "Name"))


def preserved_field(field):
    field = field.lower().replace("_", "")
    if field in ACCOUNT_FIELDS:
        return False
    return (field in PRESERVED_FIELDS or "hash" in field or "digest" in field or "sha256" in field
            or field.endswith(("count", "counts", "bytes", "size", "ms", "seconds", "timestamp", "time", "at")))


def placeholder(value):
    return (value.startswith(("AWS_", "RESOURCE_", "REDACTED", "example", "EXAMPLE"))
            or "${" in value or "<" in value or "[" in value
            or value in {"ACCOUNT", "ACCOUNT_ID", "localhost"}
            or any(token in value for token in (".example.", ".example.invalid", ":ACCOUNT:", ":ACCOUNT_ID:")))


def permitted(kind, value):
    if placeholder(value):
        return True
    if kind == "EMAIL":
        domain = value.rsplit("@", 1)[1].lower()
        return domain in {"example.com", "example.org", "example.net", "example.invalid", "users.noreply.github.com"} or domain.endswith((".invalid", ".example", ".test"))
    if kind == "AWS_ACCOUNT":
        return value in EXAMPLE_ACCOUNTS
    if kind == "AWS_ARN":
        parts = value.split(":", 5)
        masked_account = len(parts) == 6 and "*" in parts[4]
        public_product = (len(parts) == 6 and parts[2] == "securityhub"
                          and not parts[4] and parts[5].startswith("product/aws/"))
        return masked_account or public_product or ":iam::aws:" in value or any(account in value for account in EXAMPLE_ACCOUNTS)
    if kind == "AWS_RESOURCE":
        suffix = value.rsplit("-", 1)[1]
        return set(suffix) == {"0"} or suffix in {"12345678", "0123456789abcdef0"}
    if kind == "IP":
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return True
        return (address.is_loopback or address.is_unspecified or value == "169.254.169.254"
                or any(address in ipaddress.ip_network(net) for net in
                       ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")))
    return False


def permitted_match(kind, match, text):
    if permitted(kind, match.group()):
        return True
    # This is GitHub's fixed SSH transport user, only when followed by a repository path.
    # A standalone address or a different user on the same host remains an EMAIL finding.
    if kind == "EMAIL" and match.group().partition("@") == ("git", "@", "github.com"):
        separator = "/" if text[:match.start()].endswith("ssh://") else ":"
        return re.match(re.escape(separator) + r"[A-Za-z0-9_{][A-Za-z0-9_./{}-]*", text[match.end():]) is not None
    return False


def findings(text, path):
    found = set()
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if not permitted_match(kind, match, text):
                found.add((kind, match.group()))
    if path.startswith("docs/evidence/"):
        for match in IP.finditer(text):
            if not permitted("IP", match.group()):
                found.add(("IP", match.group()))
        if path.endswith(".json"):
            data = json.loads(text)

            def walk(value, field="", parent=None):
                if isinstance(value, dict):
                    for key, child in value.items():
                        walk(child, key, value)
                elif isinstance(value, list):
                    for child in value:
                        walk(child, field, parent)
                elif (isinstance(value, str) and value and resource_name_field(field, parent)
                      and not placeholder(value)):
                    found.add(("RESOURCE_NAME", value))
            walk(data)
    return found


def tracked(repo):
    return subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "-z"], stderr=subprocess.PIPE,
    ).decode().split("\0")[:-1]


def read_text(repo, name):
    repo = repo.resolve()
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("expected a repository-relative path without traversal")
    path = repo / name
    if not path.parent.resolve().is_relative_to(repo):
        raise ValueError("expected a path inside repository")
    if path.is_symlink():
        return os.readlink(path)
    if not path.is_file() or not path.resolve().is_relative_to(repo):
        raise ValueError("expected a regular file inside repository")
    if path.stat().st_size > 4 * 1024 * 1024:
        return None
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def check(repo, paths):
    errors, skipped = 0, 0
    for path in paths:
        text = read_text(repo, path)
        if text is None:
            skipped += 1
            continue
        counts = collections.Counter(kind for kind, _ in findings(text, path))
        for kind, count in sorted(counts.items()):
            print(f"{path}: {kind}: {count} distinct value(s)")
            errors += count
    if not errors:
        print("Public text privacy patterns passed; credential checks remain separate.")
    if skipped:
        print(f"Skipped {skipped} binary/oversized file(s); manual review required.")
    return int(bool(errors))


def replacement(kind, number):
    if kind == "EMAIL":
        return f"person-{number:03d}@example.invalid"
    if kind == "HOME":
        return "$HOME"
    if kind == "ENDPOINT":
        return f"service-{number:03d}.example.invalid"
    if kind == "IP":
        if number > 254:
            raise ValueError("too many example IP mappings")
        return f"192.0.2.{number}"
    return f"{kind}_{number:03d}"


def replace_identifiers(value, path, mapping, applied):
    """Replace detected token spans, never every occurrence of a discovered substring."""
    patterns = [*PATTERNS.items()] + ([("IP", IP)] if path.startswith("docs/evidence/") else [])
    spans = sorted({(m.start(), m.end(), m.group()) for kind, pattern in patterns
                    for m in pattern.finditer(value) if m.group() in mapping and not permitted_match(kind, m, value)},
                   key=lambda span: (span[0], -(span[1] - span[0])))
    pieces, end = [], 0
    for start, stop, raw in spans:
        if start < end:
            continue  # A full ARN/endpoint takes precedence over a contained identifier.
        kind, token = mapping[raw]
        pieces.extend((value[end:start], token))
        applied[kind] += 1
        end = stop
    return "".join([*pieces, value[end:]])


def rewrite_json(value, path, mapping, applied, field="", parent=None):
    if preserved_field(field):
        return value
    if isinstance(value, dict):
        return {key: rewrite_json(child, path, mapping, applied, key, value)
                for key, child in value.items()}
    if isinstance(value, list):
        return [rewrite_json(child, path, mapping, applied, field, parent) for child in value]
    if isinstance(value, str):
        if DIGEST.fullmatch(value):
            return value
        if path.startswith("docs/evidence/") and resource_name_field(field, parent) and value in mapping:
            kind, token = mapping[value]
            applied[kind] += 1
            return token
        return replace_identifiers(value, path, mapping, applied)
    if (type(value) is int and field.lower().replace("_", "") in ACCOUNT_FIELDS
            and str(value) in mapping):
        kind, token = mapping[str(value)]
        applied[kind] += 1
        return token
    return value


def verify_json_preservation(original, public, field="", parent=None):
    """Compare evidence independently of the rewrite and its redaction counts."""
    def unchanged():
        if type(original) is not type(public) or original != public:
            raise ValueError("evidence preservation check failed")

    if preserved_field(field):
        unchanged()
    elif isinstance(original, dict):
        if not isinstance(public, dict) or original.keys() != public.keys():
            raise ValueError("evidence JSON keys changed")
        for key, child in original.items():
            verify_json_preservation(child, public[key], key, original)
    elif isinstance(original, list):
        if not isinstance(public, list) or len(original) != len(public):
            raise ValueError("evidence JSON structure changed")
        for before, after in zip(original, public):  # Lengths were compared above (Python 3.9 CLI).
            verify_json_preservation(before, after, field, parent)
    elif isinstance(original, str):
        if not isinstance(public, str):
            raise ValueError("evidence JSON type changed")
        if (DIGEST.fullmatch(original) or (not resource_name_field(field, parent)
                and not any(pattern.search(original) for pattern in [*PATTERNS.values(), IP]))):
            unchanged()
    elif (type(original) is int and field.lower().replace("_", "") in ACCOUNT_FIELDS
          and isinstance(public, str) and re.fullmatch(r"AWS_ACCOUNT_[0-9]+", public)):
        if not PATTERNS["AWS_ACCOUNT"].fullmatch(str(original)):
            raise ValueError("invalid account redaction")
    else:
        unchanged()


def sanitize(repo, paths, private_root, manifest_path):
    repo = repo.resolve()
    private_root = private_root.expanduser().resolve()
    if private_root.is_relative_to(repo):
        raise ValueError("private originals must be outside repository")
    if any((repo / path).is_symlink() for path in paths):
        raise ValueError("sanitize does not rewrite symlinks")
    contents = {path: read_text(repo, path) for path in paths}
    if any(value is None for value in contents.values()):
        raise ValueError("sanitize accepts bounded UTF-8 text files only")
    matches = sorted(set().union(*(findings(text, path) for path, text in contents.items())))
    counters, mapping = collections.Counter(), {}
    for kind, value in matches:
        counters[kind] += 1
        mapping.setdefault(value, (kind, replacement(kind, counters[kind])))
    prepared = []
    for name, original in contents.items():
        applied = collections.Counter()
        if name.endswith(".json"):
            before = json.loads(original)
            rewritten = rewrite_json(before, name, mapping, applied)
            verify_json_preservation(json.loads(original), rewritten)
            public = json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n" if applied else original
        else:
            public = replace_identifiers(original, name, mapping, applied)
        if findings(public, name):
            raise ValueError("sanitized artifact still fails privacy check")
        if original != public:
            prepared.append((name, original, public, applied))
    if not prepared:
        print("No matching identifiers; artifacts and manifest unchanged.")
        return
    run = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_root.chmod(0o700)
    archive = private_root / run
    archive.mkdir(parents=True, mode=0o700)
    archive.chmod(0o700)
    source_ref = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, stderr=subprocess.PIPE,
    ).strip()
    entries = []
    for name, original, public, applied in prepared:
        backup = archive / name
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with backup.open("xb") as stream:
            stream.write(original.encode())
        backup.chmod(0o600)
        original_hash = hashlib.sha256(original.encode()).hexdigest()
        if hashlib.sha256(backup.read_bytes()).hexdigest() != original_hash:
            raise ValueError("private original verification failed")
        entries.append({"path": name, "checkout_commit": source_ref,
                        "original_sha256": original_hash,
                        "public_sha256": hashlib.sha256(public.encode()).hexdigest(),
                        "redactions": dict(applied), "public_is_raw": False})
    private_map = archive / "mapping.json"
    private_map.write_text(json.dumps(mapping, indent=2) + "\n")
    private_map.chmod(0o600)
    path = repo / manifest_path
    if not path.resolve().is_relative_to(repo):
        raise ValueError("public manifest must be inside repository")
    previous = json.loads(path.read_text()) if path.exists() else {"format_version": 1, "runs": []}
    if not isinstance(previous, dict) or not isinstance(previous.get("runs"), list):
        raise ValueError("invalid redaction manifest")  # noqa: TRY004 - JSON input validation.
    previous["runs"].append({"run": run, "originals": "preserved privately outside repository",
                              "embedded_hashes": "unchanged; identify the original measured inputs",
                              "artifacts": entries})
    for name, _, public, _ in prepared:
        (repo / name).write_text(public, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n")
    print(f"Sanitized {len(entries)} artifact(s); private originals verified; run={run}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "sanitize"))
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--manifest", default="docs/evidence/privacy/redaction-manifest.json")
    args = parser.parse_args()
    try:
        repo = args.repo.resolve()
        paths = args.paths or tracked(repo)
        if args.command == "check":
            return check(repo, paths)
        if not args.paths or args.private_root is None:
            parser.error("sanitize requires explicit paths and --private-root")
        sanitize(repo, paths, args.private_root, args.manifest)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError):
        # Tracebacks and subprocess stderr can disclose operator paths or raw input.
        print("Public evidence validation failed: input could not be safely processed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
