#!/usr/bin/env python3
"""Check the target backup bucket on SeaweedFS: versioning, the exact policy, and what each identity may do.

Run by the SeaweedFS owner with the admin, writer and reader keys in the environment
(T35_ADMIN_AK/SK, T35_WRITER_AK/SK, T35_READER_AK/SK), for example through a port-forward.
It writes one probe object under the backup prefix, tries every forbidden call with the
backup identities, checks the object is untouched, then removes it as admin. Real backups
are never touched. Prints JSON without keys. Exit codes: 0 PASS, 1 FAIL, 2 UNKNOWN.
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

EXPECTED = {
    "writer.put": "ok", "writer.get": "ok", "writer.list": "ok",
    "writer.delete": "denied", "writer.delete_version": "denied", "writer.suspend_versioning": "denied",
    "writer.delete_policy": "denied", "writer.put_policy": "denied",
    "reader.get": "ok", "reader.list": "ok", "reader.put": "denied", "reader.delete": "denied",
}


def normalized(policy):
    """SeaweedFS returns a one-element Resource list as a string; compare as sets."""
    def value(v):
        return sorted(v) if isinstance(v, list) else [v]
    return sorted(
        json.dumps({k: (value(v) if k in ("Action", "Resource") else
                        {"AWS": value(v["AWS"])} if k == "Principal" and isinstance(v, dict) else v)
                    for k, v in s.items()}, sort_keys=True)
        for s in policy["Statement"])


def client(endpoint, who, ca):
    return botocore.session.get_session().create_client(
        "s3", region_name="us-east-1", endpoint_url=endpoint, verify=ca or True,
        aws_access_key_id=os.environ[f"T35_{who}_AK"], aws_secret_access_key=os.environ[f"T35_{who}_SK"],
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


def check(endpoint, ca=None):
    admin, writer, reader = (client(endpoint, who, ca) for who in ("ADMIN", "WRITER", "READER"))
    out = {"versioning_enabled": admin.get_bucket_versioning(Bucket=BUCKET).get("Status") == "Enabled",
           "policy_matches": normalized(json.loads(admin.get_bucket_policy(Bucket=BUCKET)["Policy"]))
           == normalized(bucket_policy())}
    key = f"{PREFIX}_t35-probe/{uuid.uuid4()}"
    version = None
    try:
        version = (attempt(out, "writer.put", lambda: writer.put_object(Bucket=BUCKET, Key=key, Body=b"t35")) or {}).get("VersionId")
        attempt(out, "writer.get", lambda: writer.get_object(Bucket=BUCKET, Key=key)["Body"].read())
        attempt(out, "writer.list", lambda: writer.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX))
        attempt(out, "writer.delete", lambda: writer.delete_object(Bucket=BUCKET, Key=key))
        attempt(out, "writer.delete_version", lambda: writer.delete_object(Bucket=BUCKET, Key=key, VersionId=version or "missing"))
        attempt(out, "writer.suspend_versioning", lambda: writer.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Suspended"}))
        attempt(out, "writer.delete_policy", lambda: writer.delete_bucket_policy(Bucket=BUCKET))
        attempt(out, "writer.put_policy", lambda: writer.put_bucket_policy(
            Bucket=BUCKET, Policy=json.dumps({"Version": "2012-10-17", "Statement": []})))
        attempt(out, "reader.get", lambda: reader.get_object(Bucket=BUCKET, Key=key)["Body"].read())
        attempt(out, "reader.list", lambda: reader.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX))
        attempt(out, "reader.put", lambda: reader.put_object(Bucket=BUCKET, Key=key + ".reader", Body=b"x"))
        attempt(out, "reader.delete", lambda: reader.delete_object(Bucket=BUCKET, Key=key))
        listing = admin.list_object_versions(Bucket=BUCKET, Prefix=key)
        out["probe_intact"] = (version is not None and [v["VersionId"] for v in listing.get("Versions", [])] == [version]
                               and not listing.get("DeleteMarkers"))
    finally:
        listing = admin.list_object_versions(Bucket=BUCKET, Prefix=key)
        removed = True
        for item in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
            try:
                admin.delete_object(Bucket=BUCKET, Key=item["Key"], VersionId=item["VersionId"])
            except ClientError:
                removed = False
        out["admin_removed_probe"] = removed
    return out


def status(out):
    if any(out.get(k) is False for k in ("versioning_enabled", "policy_matches", "probe_intact")) or any(
            out.get(k) == "ok" and v == "denied" for k, v in EXPECTED.items()):
        return "FAIL"
    if all(out.get(k) == v for k, v in EXPECTED.items()) and out.get("probe_intact") and out.get("admin_removed_probe"):
        return "PASS"
    return "UNKNOWN"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="the SeaweedFS S3 address reachable from here")
    parser.add_argument("--ca", help="CA bundle for an HTTPS endpoint")
    args = parser.parse_args(argv)
    out = check(args.endpoint, args.ca)
    out["status"] = status(out)
    json.dump(out, sys.stdout, indent=2)
    print()
    return {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}[out["status"]]


if __name__ == "__main__":
    sys.exit(main())
