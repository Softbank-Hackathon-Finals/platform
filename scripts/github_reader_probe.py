"""Read a pinned demo source with an injected reader credential; never write refs."""

import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = "Softbank-Hackathon-Finals/demo-chat"
REPOSITORY_ID = 1443045289
ACTOR_ID = 191002565
REVISION = "dc9bece24898b752a77af1d1bbd440c11100e5f2"
SOURCE_PATH = "README.md"
EXPIRY = datetime(2026, 11, 11, tzinfo=UTC)
TOKEN_FILE = Path("/run/bravo/github/token")


class ProbeError(Exception):
    """Only fixed categories may leave the credential-bearing process."""


def require(condition, category):
    if not condition:
        raise ProbeError(category)


def request(token, path, expected):
    connection = http.client.HTTPSConnection("api.github.com", timeout=15,
                                             context=ssl.create_default_context())
    headers = {"User-Agent": "bravo-source-reader", "Accept": "application/vnd.github+json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        require(response.status == expected, "unexpected_http_status")
        raw = response.read(1_000_001)
        require(len(raw) <= 1_000_000, "response_too_large")
        return json.loads(raw)
    finally:
        connection.close()


def verify(token, call=request, now=None):
    require((now or datetime.now(UTC)) < EXPIRY, "credential_expired")
    require(re.fullmatch(r"github_pat_[A-Za-z0-9_]{20,250}", token), "credential_format")
    # Public repository HTTP 200 alone is insufficient: use the same credential's
    # authenticated identity and an unauthenticated control on a private endpoint.
    call(None, "/user", 401)
    user = call(token, "/user", 200)
    require(user.get("id") == ACTOR_ID, "authenticated_actor_mismatch")
    base = "/repos/" + REPOSITORY
    repo = call(token, base, 200)
    require(repo.get("id") == REPOSITORY_ID and repo.get("full_name") == REPOSITORY,
            "repository_mismatch")
    commit = call(token, base + "/git/commits/" + REVISION, 200)
    require(commit.get("sha") == REVISION, "revision_mismatch")
    source = call(token, base + "/contents/" + SOURCE_PATH + "?ref=" + REVISION, 200)
    require(source.get("type") == "file" and source.get("path") == SOURCE_PATH
            and source.get("encoding") == "base64", "source_metadata_mismatch")
    data = base64.b64decode(source["content"].replace("\n", ""), validate=True)
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    require(blob == source.get("sha") and len(data) == source.get("size"), "source_blob_mismatch")
    return {"authenticated_actor_id": ACTOR_ID, "anonymous_identity_denied": True,
            "repository": REPOSITORY, "repository_id": REPOSITORY_ID, "revision": REVISION,
            "source_path": SOURCE_PATH, "source_blob_sha": blob,
            "source_sha256": hashlib.sha256(data).hexdigest(), "source_bytes": len(data),
            "github_write_requests": 0}


def main():
    receipt = {"schema_version": 1, "status": "UNKNOWN", "role": "reader",
               "observed_at": datetime.now(UTC).isoformat(), "pod_uid": os.getenv("POD_UID"),
               "run_id": sys.argv[1] if len(sys.argv) == 2 else ""}
    try:
        require(os.getuid() == 10001 and receipt["pod_uid"]
                and re.fullmatch(r"[0-9a-f]{32}", receipt["run_id"]), "unexpected_runtime")
        with TOKEN_FILE.open() as stream:
            token = stream.read(301).strip()
        receipt.update(verify(token))
        receipt["status"] = "PASS"
    except ProbeError as error:
        receipt["category"] = str(error)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        receipt["category"] = "transport_or_response_unknown"
    print(json.dumps(receipt))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
