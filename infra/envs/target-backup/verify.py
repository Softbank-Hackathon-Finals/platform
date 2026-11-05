#!/usr/bin/env python3
"""Check the target backup bucket on SeaweedFS: protection first, then what each identity may do.

Keys come from the environment as T35_<LABEL>_AK / T35_<LABEL>_SK for ADMIN, WRITER, READER and
every --deny label (the app and the Executor are required). The run reads versioning and the
bucket policy and stops unless both are as rendered. On the registered bucket it then uses one
probe object only: the backup identities write and read it, and nobody may delete it. Calls that
would change protection if they succeeded (versioning, policy) run only with --disposable, on a
throwaway bucket. Nobody, the owner included, can delete under the policy, so the 3-byte probe stays
under `<prefix>_t35-probe/` and is reported; removing it would mean taking the policy down. Real
backups are never touched.
Exit codes: 0 PASS, 1 FAIL, 2 UNKNOWN.
"""

import argparse
import json
import os
import sys
import uuid

import botocore.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from render import BUCKET, PREFIX, bucket_policy

REQUIRED_DENY = ("app", "executor")
EXPECTED = {
    "writer.put": "ok", "writer.get": "ok", "writer.list": "ok", "writer.delete": "denied", "writer.delete_version": "denied",
    "reader.get": "ok", "reader.list": "ok", "reader.put": "denied", "reader.delete": "denied",
}
PROTECTION_CHANGES = ("suspend_versioning", "delete_policy", "put_policy")


def normalized(policy):
    """SeaweedFS returns a one-element Resource list as a string; compare as sets."""
    def value(v):
        return sorted(v) if isinstance(v, list) else [v]
    return sorted(
        json.dumps({k: (value(v) if k in ("Action", "Resource") else
                        {"AWS": value(v["AWS"])} if k == "Principal" and isinstance(v, dict) else v)
                    for k, v in s.items()}, sort_keys=True)
        for s in policy["Statement"])


def client(endpoint, label, ca):
    return botocore.session.get_session().create_client(
        "s3", region_name="us-east-1", endpoint_url=endpoint, verify=ca or True,
        aws_access_key_id=os.environ[f"T35_{label.upper()}_AK"], aws_secret_access_key=os.environ[f"T35_{label.upper()}_SK"],
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4", connect_timeout=5, read_timeout=10,
                      retries={"mode": "standard", "total_max_attempts": 1}))


def attempt(out, name, call):
    try:
        result = call()
        out[name] = "ok"
        return result
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        out[name] = "denied" if code == "AccessDenied" else "error:" + str(code)
    except BotoCoreError as error:
        out[name] = "error:" + type(error).__name__
    return None


def protection(admin):
    """True/False when the stored protection could be read, None when it could not."""
    out = {}
    try:
        out["versioning_enabled"] = admin.get_bucket_versioning(Bucket=BUCKET).get("Status") == "Enabled"
    except (ClientError, BotoCoreError):
        out["versioning_enabled"] = None
    try:
        stored = json.loads(admin.get_bucket_policy(Bucket=BUCKET)["Policy"])
        out["policy_matches"] = normalized(stored) == normalized(bucket_policy())
    except ClientError as error:
        out["policy_matches"] = False if error.response.get("Error", {}).get("Code") == "NoSuchBucketPolicy" else None
    except (BotoCoreError, ValueError, KeyError, TypeError):
        out["policy_matches"] = None
    return out


def check(endpoint, deny_labels, disposable=False, ca=None):
    admin = client(endpoint, "admin", ca)
    out = protection(admin)
    if out["versioning_enabled"] is not True or out["policy_matches"] is not True:
        return out  # never probe a bucket whose protection is missing or unreadable
    writer, reader = client(endpoint, "writer", ca), client(endpoint, "reader", ca)
    deniers = {label: client(endpoint, label, ca) for label in deny_labels}
    key = f"{PREFIX}_t35-probe/{uuid.uuid4()}"
    out["probe_left"] = key
    version = None
    try:
        version = (attempt(out, "writer.put", lambda: writer.put_object(Bucket=BUCKET, Key=key, Body=b"t35")) or {}).get("VersionId")
        attempt(out, "writer.get", lambda: writer.get_object(Bucket=BUCKET, Key=key)["Body"].read())
        attempt(out, "writer.list", lambda: writer.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX))
        attempt(out, "reader.get", lambda: reader.get_object(Bucket=BUCKET, Key=key)["Body"].read())
        attempt(out, "reader.list", lambda: reader.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX))
        attempt(out, "reader.put", lambda: reader.put_object(Bucket=BUCKET, Key=key + ".reader", Body=b"x"))
        for label, who in [("writer", writer), ("reader", reader), *deniers.items()]:
            attempt(out, f"{label}.delete_version", lambda who=who: who.delete_object(
                Bucket=BUCKET, Key=key, VersionId=version or "missing"))
            attempt(out, f"{label}.delete", lambda who=who: who.delete_object(Bucket=BUCKET, Key=key))
        if disposable:
            for label, who in [("writer", writer), *deniers.items()]:
                attempt(out, f"{label}.suspend_versioning", lambda who=who: who.put_bucket_versioning(
                    Bucket=BUCKET, VersioningConfiguration={"Status": "Suspended"}))
                attempt(out, f"{label}.delete_policy", lambda who=who: who.delete_bucket_policy(Bucket=BUCKET))
                attempt(out, f"{label}.put_policy", lambda who=who: who.put_bucket_policy(
                    Bucket=BUCKET, Policy=json.dumps({"Version": "2012-10-17", "Statement": []})))
            out["protection_after"] = protection(admin)
        listing = admin.list_object_versions(Bucket=BUCKET, Prefix=key)
        out["probe_intact"] = (version is not None and [v["VersionId"] for v in listing.get("Versions", [])] == [version]
                               and not listing.get("DeleteMarkers"))
    except (ClientError, BotoCoreError):
        out["probe_intact"] = None
    return out


def status(out, deny_labels, disposable):
    """FAIL when protection is wrong or a forbidden call succeeded; PASS only when every identity was shown refused."""
    if out.get("versioning_enabled") is False or out.get("policy_matches") is False or out.get("probe_intact") is False:
        return "FAIL"
    if out.get("versioning_enabled") is None or out.get("policy_matches") is None:
        return "UNKNOWN"
    forbidden = [k for k, v in EXPECTED.items() if v == "denied"] + [f"{label}.{call}" for label in deny_labels
                                                                       for call in ("delete", "delete_version")]
    if disposable:
        forbidden += [f"{label}.{call}" for label in ("writer", *deny_labels) for call in PROTECTION_CHANGES]
        if out.get("protection_after", {}) != {"versioning_enabled": True, "policy_matches": True}:
            return "FAIL"
    if any(out.get(name) == "ok" for name in forbidden):
        return "FAIL"
    if (any(out.get(k) != v for k, v in EXPECTED.items()) or any(out.get(name) != "denied" for name in forbidden)
            or not out.get("probe_intact")
            or any(label not in deny_labels for label in REQUIRED_DENY)):
        return "UNKNOWN"
    return "PASS"


def scope(out, deny_labels, disposable):
    """Identities whose every answer was as registered, and the rest; a run that stopped early verified nobody."""
    def calls(label):
        names = [k for k in EXPECTED if k.startswith(label + ".")] + [f"{label}.{c}" for c in ("delete", "delete_version")]
        return names + ([f"{label}.{c}" for c in PROTECTION_CHANGES] if disposable and label != "reader" else [])
    shown = {label for label in ("writer", "reader", *deny_labels)
             if all(out.get(name) == EXPECTED.get(name, "denied") for name in calls(label))}
    return sorted(shown), sorted({"writer", "reader", *REQUIRED_DENY, *deny_labels} - shown)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="the SeaweedFS S3 address reachable from here")
    parser.add_argument("--ca", help="CA bundle for an HTTPS endpoint")
    parser.add_argument("--deny", action="append", default=[], metavar="LABEL",
                        help="another identity that must not delete (app and executor are required); keys in T35_<LABEL>_AK/SK")
    parser.add_argument("--disposable", action="store_true",
                        help="throwaway bucket only: also try calls that would remove protection if they succeeded")
    args = parser.parse_args(argv)
    labels = [label.lower() for label in args.deny]
    if any(not label.isidentifier() or label in ("admin", "writer", "reader") for label in labels):
        parser.error("--deny takes labels such as app or executor")
    out = check(args.endpoint, labels, args.disposable, args.ca)
    out["verified"], out["unverified"] = scope(out, labels, args.disposable)
    out["status"] = status(out, labels, args.disposable)
    json.dump(out, sys.stdout, indent=2)
    print()
    return {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}[out["status"]]


if __name__ == "__main__":
    sys.exit(main())
