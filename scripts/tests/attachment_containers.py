"""Disposable containers for the attachment copy E2E test; every name starts with t8-attach-.

The destination runs the exact image, arguments and bucket-scoped identities that
objects.py and the target workload guide deploy, so conditional create is observed
on the real SeaweedFS version rather than a stand-in. Keys are random per run and
only ever reach the containers through environment variables, never a file on disk.
"""
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

WORKLOADS = Path(__file__).resolve().parents[2] / "infra/envs/target-workloads"
SOURCE_IMAGE = "motoserver/moto:5.1.4"
DATABASE_IMAGE = "public.ecr.aws/docker/library/postgres:18.4"
BUCKET = "demo-chat-attachments"
DATABASE_TLS_NAME = "source-db.invalid"
READY_SECONDS = 180


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, **kwargs)


def published_port(name, port):
    # Docker chooses the host port so concurrent sessions cannot collide.
    output = docker("port", name, f"{port}/tcp").stdout.decode()
    return output.splitlines()[0].rsplit(":", 1)[1].strip()


def wait_for(probe):
    deadline = time.monotonic() + READY_SECONDS
    while True:
        try:
            return probe()
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def s3_client(endpoint, key, secret):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id=key,
                        aws_secret_access_key=secret, aws_session_token=None,
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                                      request_checksum_calculation="when_required",
                                      response_checksum_validation="when_required"))


def auth_config(bootstrap, client):
    def identity(name, pair, actions):
        return {"name": name, "credentials": [{"accessKey": pair[0], "secretKey": pair[1]}],
                "actions": [f"{action}:{BUCKET}" for action in actions]}

    # The exact two identities the target workload guide requires: no anonymous, no wildcard.
    return json.dumps({"identities": [
        identity("bootstrap", bootstrap, ["Admin", "Read", "Write", "List", "Tagging"]),
        identity("client", client, ["Read", "Write", "List", "Tagging"])]})


def start_target(name):
    from botocore.exceptions import ClientError
    sys.path.append(str(WORKLOADS))
    import objects
    deployment = objects.objects("t8-attach-node")[3]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    bootstrap = (secrets.token_hex(10), secrets.token_hex(20))
    client = (secrets.token_hex(10), secrets.token_hex(20))
    # The root filesystem stays read-only as deployed, so the config lands on a tmpfs before exec.
    unpack = 'printf %s "$T8_ATTACH_S3_JSON" > /etc/seaweedfs-auth/s3.json && exec "$@"'
    docker("run", "-d", "--name", name, "--platform", "linux/amd64", "--user", "1000:1000", "--read-only",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--tmpfs", "/data:uid=1000,gid=1000", "--tmpfs", "/tmp:uid=1000,gid=1000",
           "--tmpfs", "/etc/seaweedfs-auth:uid=1000,gid=1000", "-p", "127.0.0.1::8333",
           "-e", "T8_ATTACH_S3_JSON=" + auth_config(bootstrap, client), "--entrypoint", "sh",
           container["image"], "-c", unpack, "sh", *container["command"], *container["args"])
    endpoint = "http://127.0.0.1:" + published_port(name, 8333)
    admin = s3_client(endpoint, *bootstrap)

    def writable():
        # A successful bucket HEAD is not enough: the guide warns object PUT can still lack a volume.
        try:
            admin.head_bucket(Bucket=BUCKET)
        except ClientError:
            admin.create_bucket(Bucket=BUCKET)
        admin.put_object(Bucket=BUCKET, Key="readiness", Body=b"ready")
        admin.delete_object(Bucket=BUCKET, Key="readiness")

    wait_for(writable)
    return {"target_endpoint": endpoint, "target_image": container["image"],
            "target_bootstrap": bootstrap, "target_client": client}


def start_source(name):
    docker("run", "-d", "--name", name, "-p", "127.0.0.1::5000", SOURCE_IMAGE)
    endpoint = "http://127.0.0.1:" + published_port(name, 5000)
    pair = (secrets.token_hex(10), secrets.token_hex(20))
    wait_for(lambda: s3_client(endpoint, *pair).create_bucket(Bucket=BUCKET))
    return {"source_endpoint": endpoint, "source_pair": pair, "source_image": SOURCE_IMAGE}


def start_database(name):
    import psycopg
    password = secrets.token_hex(20)
    # A self-signed certificate for a fixed name lets real verify-full run over loopback.
    prepare = (f"openssl req -new -x509 -days 2 -nodes -subj /CN={DATABASE_TLS_NAME} "
               f"-addext subjectAltName=DNS:{DATABASE_TLS_NAME} -keyout /tmp/server.key -out /tmp/server.crt "
               "&& chown postgres /tmp/server.key /tmp/server.crt && chmod 600 /tmp/server.key "
               "&& exec docker-entrypoint.sh postgres -c ssl=on "
               "-c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key")
    docker("run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=" + password, "-e", "POSTGRES_DB=chat",
           "-p", "127.0.0.1::5432", "--entrypoint", "sh", DATABASE_IMAGE, "-c", prepare)
    port = published_port(name, 5432)
    database = {"host": "127.0.0.1", "port": port, "user": "postgres", "password": password, "dbname": "chat"}

    def apply_schema():
        with psycopg.connect(**database, autocommit=True, connect_timeout=2) as connection:
            connection.execute("CREATE ROLE chat_app NOLOGIN")
            connection.execute("CREATE ROLE chat_replicator LOGIN REPLICATION")
            connection.execute((WORKLOADS / "schema.sql").read_text())

    wait_for(apply_schema)
    certificate = docker("exec", name, "cat", "/tmp/server.crt").stdout.decode()
    return {"database": database, "database_image": DATABASE_IMAGE, "database_tls_name": DATABASE_TLS_NAME,
            "database_ca": certificate}


def start():
    run = secrets.token_hex(4)
    names = {role: f"t8-attach-{role}-{run}" for role in ("source", "target", "database")}
    environment = {"names": list(names.values())}
    try:
        environment.update(start_source(names["source"]))
        environment.update(start_target(names["target"]))
        environment.update(start_database(names["database"]))
    except BaseException:
        stop(environment)
        raise
    return environment


def stop(environment):
    # Only the names this run created; other sessions' containers are never matched by pattern.
    for name in environment["names"]:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
