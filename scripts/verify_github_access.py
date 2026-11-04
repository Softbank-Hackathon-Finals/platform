"""Verify two repository credentials with isolated, temporary Git references.

Credentials enter through stdin, stay in memory and never enter the receipt.
This checks T7 access provisioning, not T31 approval or deployment behavior.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

API = "https://api.github.com"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ObservationError(Exception):
    """A fixed error category, with no provider body or credential."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, repository, token):
        self.base = f"{API}/repos/{repository}"
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, payload=None):
        request = urllib.request.Request(
            self.base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2026-03-10"},
        )
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, ValueError):
            raise ObservationError("transport_unknown") from None
        with response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ObservationError("response_too_large")
            try:
                body = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                raise ObservationError("response_invalid") from None
            if not isinstance(body, dict):
                raise ObservationError("response_invalid")
            return response.code, body, dict(response.headers.items())


def require(response, status, category):
    if response[0] != status:
        raise ObservationError(category)
    return response[1]


def permission_denied(response):
    status, body, headers = response
    headers = {key.lower(): value for key, value in headers.items()}
    return (status == 403
            and body.get("message") == "Resource not accessible by personal access token"
            and headers.get("x-ratelimit-remaining", "0").isdigit()
            and int(headers.get("x-ratelimit-remaining", "0")) > 0
            and "contents=write" in headers.get("x-accepted-github-permissions", ""))


def verify(repository, repository_id, reader, writer, probe_id=None):
    probe_id = probe_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", probe_id):
        raise ValueError("invalid probe id")
    result = {"schema_version": 1, "observed_at": datetime.now(UTC).isoformat(),
              "repository": repository, "repository_id": repository_id,
              "status": "UNKNOWN", "checks": {}, "probe_refs": [],
              "cleanup_verified": False, "t31_consumer_verified": False}
    checks = result["checks"]
    owned = {}
    base_sha = None
    try:
        for role, client in (("reader", reader), ("writer", writer)):
            info = require(client.request("GET", ""), 200, f"{role}_repository_unknown")
            if info.get("id") != repository_id or info.get("full_name") != repository:
                raise ObservationError("repository_identity_mismatch")
            if role == "writer":
                default = info.get("default_branch")
                if not isinstance(default, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", default):
                    raise ObservationError("default_branch_invalid")
        ref = require(writer.request("GET", f"/git/ref/heads/{default}"), 200, "base_read_unknown")
        base_sha = ref.get("object", {}).get("sha")
        if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
            raise ObservationError("base_sha_invalid")
        result["base_revision"] = base_sha
        for role, client in (("reader", reader), ("writer", writer)):
            commit = require(client.request("GET", f"/git/commits/{base_sha}"), 200,
                             f"{role}_contents_read_unknown")
            checks[f"{role}_contents_read"] = commit.get("sha") == base_sha
            if not checks[f"{role}_contents_read"]:
                raise ObservationError("commit_identity_mismatch")

        # A successful writer operation brackets the reader's denial observation.
        # Only new, unpredictable references are touched; default branches are read only.
        for role, client in (("writer_before", writer), ("reader", reader), ("writer_after", writer)):
            name = f"heads/t7-access-probe/{probe_id}-{role}"
            result["probe_refs"].append(name)
            require(writer.request("GET", f"/git/ref/{name}"), 404, "probe_ref_already_exists")
            # Track before dispatch: a lost response must still trigger cleanup/readback.
            owned[name] = base_sha
            response = client.request("POST", "/git/refs", {"ref": f"refs/{name}", "sha": base_sha})
            if role == "reader":
                checks["reader_write_denied"] = permission_denied(response)
                if response[0] != 201 and not checks["reader_write_denied"]:
                    raise ObservationError("reader_denial_unknown")
            else:
                created = require(response, 201, f"{role}_write_unknown")
                if created.get("ref") != f"refs/{name}" or created.get("object", {}).get("sha") != base_sha:
                    raise ObservationError("created_ref_mismatch")
                observed = require(reader.request("GET", f"/git/ref/{name}"), 200, "created_ref_read_unknown")
                checks[role] = observed.get("object", {}).get("sha") == base_sha
                if not checks[role]:
                    raise ObservationError("created_ref_read_mismatch")
        result["status"] = "PASS" if all(checks.values()) else "FAIL"
    except ObservationError as error:
        result["error_category"] = str(error)
    finally:
        cleanup = []
        for name, expected in owned.items():
            try:
                response = writer.request("GET", f"/git/ref/{name}")
                if response[0] == 404:
                    cleanup.append(True)
                    continue
                current = require(response, 200, "cleanup_read_unknown")
                # Never remove a branch that another writer has advanced.
                if current.get("object", {}).get("sha") != expected:
                    cleanup.append(False)
                    continue
                require(writer.request("DELETE", f"/git/refs/{name}"), 204, "cleanup_delete_unknown")
                cleanup.append(writer.request("GET", f"/git/ref/{name}")[0] == 404)
            except ObservationError:
                cleanup.append(False)
        result["cleanup_verified"] = bool(owned) and all(cleanup)
        if not result["cleanup_verified"]:
            result["status"] = "UNKNOWN"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-id", required=True, type=int)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--write-probe", action="store_true", required=True,
                        help="Create and remove only new t7-access-probe references.")
    args = parser.parse_args()
    if not REPO.fullmatch(args.repository) or args.repository_id <= 0:
        parser.error("repository identity required")
    # Reserve a new receipt before any mutation; never overwrite prior evidence.
    with args.receipt.open("x", encoding="utf-8") as output:
        result = {"status": "UNKNOWN", "error_category": "credentials_invalid"}
        try:
            credentials = json.loads(sys.stdin.read(16_385))
            tokens = [credentials[role] for role in ("reader", "writer")]
            if (set(credentials) != {"reader", "writer"} or tokens[0] == tokens[1]
                    or not all(isinstance(token, str) and re.fullmatch(r"github_pat_[A-Za-z0-9_]{20,250}", token)
                               for token in tokens)):
                raise ValueError("credentials_invalid")
            result = verify(args.repository, args.repository_id,
                            Client(args.repository, tokens[0]), Client(args.repository, tokens[1]))
        except (KeyError, TypeError, ValueError):
            pass
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return {"PASS": 0, "FAIL": 1}.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
