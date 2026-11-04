"""Opt-in local Docker qualification of the destination audit store (ADR-0016).

SeaweedFS 4.52 (the T30 pinned image) holds OTLP JSON audit exports in an Object Lock
bucket. A write-only identity puts objects, a read-only identity lists and reads them,
and a COMPLIANCE default retention keeps every version. The OpenTelemetry Collector
0.164.0 awss3 exporter writes through HTTPS with a private CA.

Run from the package with PYTHONPATH=src. No cloud credentials, published ports or
external network: the containers share one internal Docker network. Keys and the CA are
generated per run under --private-output and never printed.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from string import Template
from uuid import uuid4

SEAWEEDFS = "docker.io/chrislusf/seaweedfs:4.52@sha256:a54ba35b44895a449f5cdd415999062201b17b9835ec0ffc19ea605b72ffa3d1"
COLLECTOR = "otel/opentelemetry-collector-contrib@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b"
PYTHON = "python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
BOTO3 = "boto3==1.40.70"
BUCKET = "bravo-audit"
PREFIX = "audit/onprem"
WRITER = "arn:aws:iam::000000000000:user/audit-writer"
READER = "arn:aws:iam::000000000000:user/audit-reader"

# What the store must guarantee, and the two hazards the search adapter must handle.
EXPECTED = {
    "admin.create_bucket_object_lock": "ok",
    "admin.put_default_retention": "ok",
    "versioning": "Enabled",
    "admin.put_bucket_policy": "ok",
    "writer.put_if_none_match": "ok",
    "writer.put_existing_key_if_none_match": "denied:PreconditionFailed",
    "writer.delete_object": "denied:AccessDenied",
    "writer.delete_version": "denied:AccessDenied",
    "writer.suspend_versioning": "denied:AccessDenied",
    "writer.delete_bucket_policy": "denied:AccessDenied",
    "writer.get_object": "denied:AccessDenied",
    "writer.list": "denied:AccessDenied",
    "reader.list": "ok",
    "reader.list_versions": "ok",
    "reader.get_version": "ok",
    "reader.put_object": "denied:AccessDenied",
    "reader.delete_object": "denied:AccessDenied",
    "admin.delete_locked_version": "denied:AccessDenied",
    "anonymous.list": "denied:AccessDenied",
    "hazard.unconditional_overwrite_adds_version": True,
    "hazard.delete_marker_hides_from_plain_list": True,
    "original_version_intact": True,
    "collector.objects": 1,
    "collector.otlp_records": 1,
    "collector.retention_mode": "COMPLIANCE",
    "collector.admin_delete_version": "denied:AccessDenied",
}


def command(*args, timeout=120, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout, **kwargs).stdout


def certificates(root):
    conf = root / "openssl.cnf"
    conf.write_text("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=audit-store\n"
                    "[ext]\nsubjectAltName=DNS:audit-store\nextendedKeyUsage=serverAuth\n")
    command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Audit store test CA",
            "-keyout", str(root / "ca.key"), "-out", str(root / "ca.crt"))
    command("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", str(conf),
            "-keyout", str(root / "tls.key"), "-out", str(root / "tls.csr"))
    command("openssl", "x509", "-req", "-in", str(root / "tls.csr"), "-CA", str(root / "ca.crt"), "-CAkey", str(root / "ca.key"),
            "-CAcreateserial", "-days", "1", "-extfile", str(conf), "-extensions", "ext", "-out", str(root / "tls.crt"))
    (root / "ca.key").unlink()
    for path in root.iterdir():
        path.chmod(0o644 if path.suffix == ".crt" else 0o600)
    (root / "tls.key").chmod(0o644)  # read by the store's non-root user inside the throwaway run only


def identities(root):
    keys = {p: (secrets.token_hex(10).upper(), secrets.token_hex(20)) for p in ("ADMIN", "WRITER", "READER")}
    actions = {"ADMIN": [f"{a}:{BUCKET}" for a in ("Admin", "Read", "Write", "List", "Tagging")],
               "WRITER": [f"Write:{BUCKET}"], "READER": [f"Read:{BUCKET}", f"List:{BUCKET}"]}
    names = {"ADMIN": "admin", "WRITER": "audit-writer", "READER": "audit-reader"}
    config = {"identities": [{"name": names[p], "credentials": [{"accessKey": ak, "secretKey": sk}], "actions": actions[p]}
                             for p, (ak, sk) in keys.items()]}
    (root / "s3.json").write_text(json.dumps(config))
    (root / "s3.json").chmod(0o644)
    env = "".join(f"{p}_AK={ak}\n{p}_SK={sk}\n" for p, (ak, sk) in keys.items())
    (root / "keys.env").write_text(env)
    # The exporter reads only the standard AWS variables, and only the writer key.
    ak, sk = keys["WRITER"]
    (root / "writer.env").write_text(f"AWS_ACCESS_KEY_ID={ak}\nAWS_SECRET_ACCESS_KEY={sk}\n")
    return keys


def probe(phase):
    """Runs inside the probe image. Prints a JSON result; never prints keys."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError

    endpoint = os.environ.get("AUDIT_STORE_ENDPOINT", "https://audit-store:8333")

    def client(prefix):
        if prefix + "_AK" not in os.environ:
            return None  # a Kubernetes job receives only its own identity
        return boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                            aws_access_key_id=os.environ[prefix + "_AK"], aws_secret_access_key=os.environ[prefix + "_SK"],
                            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 1}))

    admin, writer, reader = client("ADMIN"), client("WRITER"), client("READER")
    anonymous = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                             config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"}, retries={"max_attempts": 1}))
    out = {}
    # A new Kubernetes Pod can be refused until its address reaches the NetworkPolicy allow set.
    # Any HTTP answer, including 403, proves the path is open; only connection errors are retried.
    deadline = time.monotonic() + 60
    while True:
        try:
            anonymous.list_objects_v2(Bucket=BUCKET)
            break
        except ClientError:
            break
        except (EndpointConnectionError, ConnectionClosedError):
            if time.monotonic() > deadline:
                raise
            time.sleep(2)

    def attempt(name, fn):
        try:
            result = fn()
            out[name] = "ok"
            return result
        except ClientError as e:
            out[name] = "denied:" + e.response.get("Error", {}).get("Code", "?")
        return None

    if phase == "setup":
        attempt("admin.create_bucket_object_lock", lambda: admin.create_bucket(Bucket=BUCKET, ObjectLockEnabledForBucket=True))
        attempt("admin.put_default_retention", lambda: admin.put_object_lock_configuration(
            Bucket=BUCKET, ObjectLockConfiguration={"ObjectLockEnabled": "Enabled",
                                                    "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 1}}}))
        out["versioning"] = (attempt("admin.get_versioning", lambda: admin.get_bucket_versioning(Bucket=BUCKET)) or {}).get("Status")
        scope = [Template(s).substitute(bucket=BUCKET) for s in ("arn:aws:s3:::${bucket}", "arn:aws:s3:::${bucket}/*")]
        policy = {"Version": "2012-10-17", "Statement": [
            {"Sid": "WriterCannotRemoveOrReconfigure", "Effect": "Deny", "Principal": {"AWS": WRITER}, "Resource": scope,
             "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutBucketVersioning", "s3:PutObjectRetention",
                        "s3:PutObjectLegalHold", "s3:BypassGovernanceRetention", "s3:PutBucketPolicy",
                        "s3:DeleteBucketPolicy", "s3:PutBucketObjectLockConfiguration"]},
            {"Sid": "ReaderIsReadOnly", "Effect": "Deny", "Principal": {"AWS": READER}, "Resource": scope,
             "Action": ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutBucketVersioning", "s3:PutBucketPolicy"]}]}
        attempt("admin.put_bucket_policy", lambda: admin.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(policy)))
    elif phase == "rights":
        key, body = f"{PREFIX}/2026/11/04/00/00/rights-check.json", b'{"resourceLogs":[]}'
        before = time.time()
        put = attempt("writer.put_if_none_match", lambda: writer.put_object(Bucket=BUCKET, Key=key, Body=body, IfNoneMatch="*"))
        after = time.time()
        version = (put or {}).get("VersionId", "missing")
        attempt("writer.put_existing_key_if_none_match", lambda: writer.put_object(Bucket=BUCKET, Key=key, Body=b"x", IfNoneMatch="*"))
        attempt("writer.delete_object", lambda: writer.delete_object(Bucket=BUCKET, Key=key))
        attempt("writer.delete_version", lambda: writer.delete_object(Bucket=BUCKET, Key=key, VersionId=version))
        attempt("writer.suspend_versioning", lambda: writer.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Suspended"}))
        attempt("writer.delete_bucket_policy", lambda: writer.delete_bucket_policy(Bucket=BUCKET))
        attempt("writer.get_object", lambda: writer.get_object(Bucket=BUCKET, Key=key))
        attempt("writer.list", lambda: writer.list_objects_v2(Bucket=BUCKET))
        listed = attempt("reader.list", lambda: reader.list_objects_v2(Bucket=BUCKET, Prefix=key))
        stamp = (listed or {}).get("Contents", [{}])[0].get("LastModified")
        if stamp:
            # S3 LastModified has second precision: the store time lies in [stamp, stamp + 1 s).
            out["last_modified_bounds_put"] = stamp.timestamp() <= after and stamp.timestamp() + 1 >= before
        attempt("reader.list_versions", lambda: reader.list_object_versions(Bucket=BUCKET, Prefix=key))
        got = attempt("reader.get_version", lambda: reader.get_object(Bucket=BUCKET, Key=key, VersionId=version))
        out["reader.version_matches"] = bool(got) and got["Body"].read() == body
        attempt("reader.put_object", lambda: reader.put_object(Bucket=BUCKET, Key=f"{PREFIX}/reader.json", Body=b"x"))
        attempt("reader.delete_object", lambda: reader.delete_object(Bucket=BUCKET, Key=key))
        attempt("admin.delete_locked_version", lambda: admin.delete_object(Bucket=BUCKET, Key=key, VersionId=version))
        attempt("anonymous.list", lambda: anonymous.list_objects_v2(Bucket=BUCKET))
        # Hazards: neither removes the original version, but both change what a plain listing shows.
        writer.put_object(Bucket=BUCKET, Key=key, Body=b'{"tampered":true}')
        versions = reader.list_object_versions(Bucket=BUCKET, Prefix=key).get("Versions", [])
        out["hazard.unconditional_overwrite_adds_version"] = len(versions) == 2
        admin.delete_object(Bucket=BUCKET, Key=key)
        plain = reader.list_objects_v2(Bucket=BUCKET, Prefix=key).get("KeyCount", 0)
        markers = reader.list_object_versions(Bucket=BUCKET, Prefix=key).get("DeleteMarkers", [])
        out["hazard.delete_marker_hides_from_plain_list"] = plain == 0 and len(markers) == 1
        original = reader.get_object(Bucket=BUCKET, Key=key, VersionId=version)["Body"].read()
        out["original_version_intact"] = original == body
    elif phase == "collector":
        versions = [v for v in reader.list_object_versions(Bucket=BUCKET, Prefix=PREFIX + "/").get("Versions", [])
                    if "/logs_" in v["Key"]]
        out["collector.objects"] = len(versions)
        records = 0
        for v in versions:
            doc = json.loads(reader.get_object(Bucket=BUCKET, Key=v["Key"], VersionId=v["VersionId"])["Body"].read())
            records += sum(len(s.get("logRecords", [])) for r in doc.get("resourceLogs", []) for s in r.get("scopeLogs", []))
            retention = admin.get_object_retention(Bucket=BUCKET, Key=v["Key"], VersionId=v["VersionId"])
            out["collector.retention_mode"] = retention.get("Retention", {}).get("Mode")
            attempt("collector.admin_delete_version", lambda v=v: admin.delete_object(Bucket=BUCKET, Key=v["Key"], VersionId=v["VersionId"]))
        out["collector.otlp_records"] = records
    elif phase == "search":
        # Search identity only: every version is read once; extra versions or delete markers are tamper evidence.
        listing = reader.list_object_versions(Bucket=BUCKET, Prefix=PREFIX + "/")
        versions, markers = listing.get("Versions", []), listing.get("DeleteMarkers", [])
        keys = [v["Key"] for v in versions]
        out["search.complete"] = not listing.get("IsTruncated", False)
        out["search.objects"] = len(versions)
        out["search.tamper_evidence"] = bool(markers) or len(keys) != len(set(keys))
        out["search.otlp_records"] = sum(
            len(s.get("logRecords", []))
            for v in versions
            for r in json.loads(reader.get_object(Bucket=BUCKET, Key=v["Key"], VersionId=v["VersionId"])["Body"].read()).get("resourceLogs", [])
            for s in r.get("scopeLogs", []))
        attempt("search.put_object", lambda: reader.put_object(Bucket=BUCKET, Key=f"{PREFIX}/search.json", Body=b"x"))
    print(json.dumps(out))


def run(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    for name in ("tls", "data"):
        (root / name).mkdir()
    (root / "data").chmod(0o777)  # the store runs as a fixed non-root user inside the throwaway run
    certificates(root / "tls")
    identities(root)
    collector = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "exporters": {"awss3": {"s3uploader": {
            "region": "us-east-1", "s3_bucket": BUCKET, "s3_prefix": PREFIX, "s3_partition_format": "%Y/%m/%d/%H/%M",
            "endpoint": "https://audit-store:8333", "s3_force_path_style": True, "unique_key_func_name": "uuidv7"},
            "marshaler": "otlp_json"}},
        "service": {"pipelines": {"logs": {"receivers": ["otlp"], "exporters": ["awss3"]}},
                    "telemetry": {"logs": {"level": "error"}}},
    }
    (root / "collector.json").write_text(json.dumps(collector))
    (root / "collector.json").chmod(0o644)
    suffix = uuid4().hex[:12]
    net, store, col, image = (f"bravo-audit-store-{suffix}-{x}" for x in ("net", "store", "collector", "probe"))
    image = image.replace("-probe", ":probe")
    created = []
    result = {"images": {"seaweedfs": SEAWEEDFS, "collector": COLLECTOR, "python": PYTHON, "boto3": BOTO3},
              "tls": "private CA, server name audit-store", "cloud_resources_changed": False}
    try:
        command("docker", "build", "-q", "-t", image, "-", input=f"FROM {PYTHON}\nRUN pip install --no-cache-dir {BOTO3}\n", timeout=600)
        created.append(("image", image))
        command("docker", "network", "create", "--internal", net)
        created.append(("network", net))
        hardened = ["--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=512m"]
        command("docker", "run", "-d", "--name", store, "--network", net, "--network-alias", "audit-store", *hardened,
                "--user", "1000:1000", "--platform", "linux/amd64", "--tmpfs", "/tmp",
                "-v", f"{root}/data:/data", "-v", f"{root}/s3.json:/etc/seaweedfs/s3.json:ro", "-v", f"{root}/tls:/tls:ro",
                SEAWEEDFS, "-logtostderr=true", "server", "-dir=/data", "-ip.bind=0.0.0.0", "-s3", "-s3.port=8333",
                "-s3.config=/etc/seaweedfs/s3.json", "-s3.cert.file=/tls/tls.crt", "-s3.key.file=/tls/tls.key",
                "-s3.port.iceberg=0", "-s3.port.lance=0")
        created.append(("container", store))

        def run_probe(phase):
            out = command("docker", "run", "--rm", "--network", net, *hardened, "--user", "65534:65534",
                          "--env-file", str(root / "keys.env"), "-e", "AWS_CA_BUNDLE=/tls/ca.crt",
                          "-v", f"{Path(__file__).resolve()}:/probe.py:ro", "-v", f"{root}/tls/ca.crt:/tls/ca.crt:ro",
                          image, "python", "/probe.py", "--probe", phase)
            return json.loads(out.strip().splitlines()[-1])

        deadline = time.monotonic() + 120
        while True:
            try:
                result.update(run_probe("setup"))
                break
            except subprocess.CalledProcessError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(3)
        result.update(run_probe("rights"))
        command("docker", "run", "-d", "--name", col, "--network", net, *hardened, "--user", "65534:65534",
                "--env-file", str(root / "writer.env"), "-e", "AWS_CA_BUNDLE=/tls/ca.crt",
                "-v", f"{root}/collector.json:/etc/collector.json:ro", "-v", f"{root}/tls/ca.crt:/tls/ca.crt:ro",
                COLLECTOR, "--config=/etc/collector.json")
        created.append(("container", col))
        now = time.time_ns()
        body = json.dumps({"resourceLogs": [{"resource": {"attributes": [
            {"key": "bravo.audit.source", "value": {"stringValue": "cnpg"}}]},
            "scopeLogs": [{"logRecords": [{"timeUnixNano": str(now), "body": {"stringValue": "synthetic audit record"}}]}]}]})
        deadline = time.monotonic() + 60
        while True:
            try:
                command("docker", "run", "--rm", "--network", net, *hardened, "--user", "65534:65534", image, "python", "-c",
                        "import sys,urllib.request;r=urllib.request.urlopen(urllib.request.Request('http://"
                        + col + ":4318/v1/logs',data=sys.argv[1].encode(),headers={'Content-Type':'application/json'}),timeout=5);"
                        "sys.exit(0 if r.status==200 else 1)", body)
                break
            except subprocess.CalledProcessError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(2)
        deadline = time.monotonic() + 60
        while True:
            observed = run_probe("collector")
            if observed.get("collector.objects") or time.monotonic() > deadline:
                result.update(observed)
                break
            time.sleep(3)
        logs = subprocess.run(["docker", "logs", col], capture_output=True, text=True, timeout=30)
        result["collector_errors_logged"] = "error" in (logs.stdout + logs.stderr).lower()
        failures = {k: (result.get(k), v) for k, v in EXPECTED.items() if result.get(k) != v}
        for k in ("last_modified_bounds_put", "reader.version_matches"):
            if result.get(k) is not True:
                failures[k] = (result.get(k), True)
        result["failures"] = failures
        result["passed"] = not failures
    finally:
        for kind, name in reversed(created):
            if kind == "container":
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            elif kind == "network":
                subprocess.run(["docker", "network", "rm", name], capture_output=True)
            elif kind == "image":
                subprocess.run(["docker", "image", "rm", "-f", name], capture_output=True)
        (root / "keys.env").unlink(missing_ok=True)
        (root / "writer.env").unlink(missing_ok=True)
    (root / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-output", type=Path)
    parser.add_argument("--probe", choices=["setup", "rights", "collector", "search"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        probe(args.probe)
        return 0
    if args.private_output is None:
        parser.error("--private-output is required")
    result = run(args.private_output)
    print(json.dumps({k: result[k] for k in ("passed", "failures")}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
