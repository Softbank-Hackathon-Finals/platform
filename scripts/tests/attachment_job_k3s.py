"""Run the rendered attachment copy Job on a disposable k3s and read it with the real verdict command.

Manual; not part of CI. No cloud resource is called and no user kubeconfig is read. Every container,
network and image this creates is named t8-job-* and removed at the end. Random stand-in keys stay
in memory and in the disposable cluster.

    uv run --no-project --python 3.12 --with boto3==1.43.123 \
        python scripts/tests/attachment_job_k3s.py --out result.json

What is real: k3s with Pod Security and NetworkPolicy enforced, the rendered Job and image, objects.py's
SeaweedFS image and arguments, libpq verify-full against PostgreSQL with the repository schema and
role, and the operator's Secret recipe from the operations guide.

The copy image is the operator's OCI index and archive from attachment_image.py, imported with the guide's
ctr arguments. It is built for this machine's architecture: containerd does not start a linux/amd64-only
index on an arm64 node, and an emulated amd64 node has no seccomp, which the restricted Job requires.

What stands in: MinIO answers as s3.<region>.amazonaws.com through hostAliases and a test CA mounted
over the SDK's bundle, so the Job's environment stays as rendered and no test endpoint is set. MinIO's
STS and session policy stand in for AWS STS and IAM. A selector-less Service stands in for the Tailscale
egress route to the database.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKLOADS = REPO / "infra/envs/target-workloads"
GUIDE = REPO / "docs/operations/t8-attachment-copy-job.md"
sys.path.insert(0, str(WORKLOADS))
sys.path.insert(0, str(REPO / "scripts"))

import attachment_containers
import attachment_image
import attachment_job
import objects as object_store
import render

PREFIX = "t8-job-"
NETWORK = PREFIX + "net"
SUBNET = "10.128.8.0/24"
S3_CONTAINER, S3_IP = PREFIX + "s3", "10.128.8.10"
DB_CONTAINER, DB_IP = PREFIX + "db", "10.128.8.11"
K3S_CONTAINER, K3S_IP = PREFIX + "k3s", "10.128.8.12"
K3S_IMAGE = "mirror.gcr.io/rancher/k3s:v1.35.5-k3s1"
MINIO_IMAGE = "cgr.dev/chainguard/minio@sha256:3c60a2a8c4d33409a9bc7a623a7fcbfb4605adc3d89422d2f53b3f673a6e5597"
DB_IMAGE = "public.ecr.aws/docker/library/postgres:18.4"
STORE_REFERENCE = "docker.io/library/t8-job-store:e2e"
COPY_TAG = PREFIX + "copy:e2e"
BROKEN_TAG = PREFIX + "broken:e2e"
NS = render.NAMESPACE

REGION = "ap-northeast-2"
S3_HOST = f"s3.{REGION}.amazonaws.com"
SOURCE_BUCKET = "t8-job-source-123456789012"
BUCKET_OWNER = "123456789012"
SOURCE_BUCKET_ARN = "arn:aws:s3:::t8-job-source-123456789012"
SOURCE_KMS_KEY_ARN = "arn:aws:kms:ap-northeast-2:123456789012:key/0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f"
BOTOCORE_BUNDLE = "/usr/local/lib/python3.12/site-packages/botocore/cacert.pem"
OBJECTS = {"attachments/한글 파일 공백.bin": bytes(range(256)) * 4, "attachments/empty": b"",
           "attachments/plain.txt": b"hello\n"}
PLAN_ID = "Plan_T8-job"
EPOCH = 3
EXTRA_KEY = "readiness-canary/object-v1.txt"
EXTRA_BODY = b"prior readiness object outside the database\n"
PYTHON_SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                   "capabilities": {"drop": ["ALL"]}}

checks = []


def check(name, passed, **observed):
    checks.append({"name": name, "passed": bool(passed), **observed})
    print(("ok   " if passed else "FAIL ") + name + (" " + json.dumps(observed, ensure_ascii=False) if observed else ""),
          flush=True)


def run(*command, stdin=None, check_exit=True, timeout=300, env=None):
    binary = isinstance(stdin, bytes)
    done = subprocess.run(command, input=stdin, capture_output=True, text=not binary, timeout=timeout,
                          check=False, env=env)
    if check_exit and done.returncode:
        stderr = done.stderr.decode(errors="replace") if binary else done.stderr
        raise RuntimeError(f"{' '.join(map(str, command[:5]))} failed: {stderr[-800:]}")
    return done


def docker(*arguments, **options):
    return run("docker", *arguments, **options)


def guarded(name):
    if not name.startswith(PREFIX):
        raise ValueError("refusing to touch " + name)
    return name


def cleanup():
    for name in (K3S_CONTAINER, S3_CONTAINER, DB_CONTAINER):
        docker("rm", "-f", "-v", guarded(name), check_exit=False)
    docker("network", "rm", guarded(NETWORK), check_exit=False)
    for tag in (COPY_TAG, BROKEN_TAG):
        docker("image", "rm", "-f", guarded(tag), check_exit=False)
    docker("image", "rm", STORE_REFERENCE, check_exit=False)


def wait_for(probe, seconds, what):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = probe()
            if value:
                return value
        except Exception:
            if time.monotonic() > deadline:
                raise
        if time.monotonic() > deadline:
            raise TimeoutError("timed out waiting for " + what)
        time.sleep(1)


def issue_certificates(directory):
    def openssl(*arguments):
        run("openssl", *arguments, timeout=60)

    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{directory}/ca.key",
            "-out", f"{directory}/ca.crt", "-subj", "/CN=t8-job-stand-in-ca", "-days", "2",
            "-addext", "basicConstraints=critical,CA:TRUE")

    def leaf(name, names):
        extension = Path(directory, name + ".ext")
        extension.write_text("subjectAltName=" + ",".join(names) + "\n")
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{directory}/{name}.key",
                "-out", f"{directory}/{name}.csr", "-subj", "/CN=" + name)
        openssl("x509", "-req", "-in", f"{directory}/{name}.csr", "-CA", f"{directory}/ca.crt",
                "-CAkey", f"{directory}/ca.key", "-CAcreateserial", "-days", "2",
                "-out", f"{directory}/{name}.crt", "-extfile", str(extension))

    leaf("s3", [f"DNS:{S3_HOST}", f"DNS:*.{S3_HOST}", "IP:127.0.0.1"])
    leaf("db", ["DNS:source-db-rw.source-dev.svc"])


def build_images(directory):
    dockerfile = WORKLOADS / "attachment-copy.Dockerfile"
    base = re.search(r"(?m)^FROM (\S+)$", dockerfile.read_text()).group(1)
    # The same digest from a public mirror: Docker Hub credentials on an operator machine may be expired.
    context = f"docker.io/library/{base}=docker-image://mirror.gcr.io/library/{base}"
    docker("buildx", "build", "--load", "-t", COPY_TAG, "-f", str(dockerfile), "--build-context", context, str(WORKLOADS),
           timeout=600)
    broken = Path(directory, "broken.Dockerfile")
    broken.write_text(f"FROM {COPY_TAG}\nUSER 0:0\nRUN rm /app/sync_attachments.py\nUSER 10001:10001\n")
    docker("buildx", "build", "--load", "-t", BROKEN_TAG, "-f", str(broken), directory, timeout=300)


def build_copy_archive(directory):
    """The operator's build command and packing, for this node's architecture."""
    architecture = docker("version", "--format", "{{.Server.Arch}}").stdout.strip()
    source = run("git", "-C", str(REPO), "rev-parse", "HEAD").stdout.strip()
    layout, metadata = Path(directory, "copy-layout"), Path(directory, "copy-metadata.json")
    base = attachment_image.pinned_base((WORKLOADS / attachment_image.DOCKERFILE).read_text())
    run(*attachment_image.build_command(source, base, WORKLOADS, layout, metadata, "linux/" + architecture), timeout=600)
    digest = json.loads(metadata.read_text())["containerimage.digest"]
    archive = Path(directory, "copy-image.tar")
    attachment_image.pack(layout, archive)
    return archive, digest, source


def import_copy_archive(archive, digest, source):
    """Follow the guide: import, then add the digest name only if import did not create it."""
    def images():
        return docker("exec", K3S_CONTAINER, "ctr", "-n", "k8s.io", "images", "ls").stdout

    pinned = f"{attachment_image.IMAGE_NAME}@{digest}"
    docker("cp", str(archive), K3S_CONTAINER + ":/tmp/attachment-copy.tar")
    docker("exec", K3S_CONTAINER, "ctr", "-n", "k8s.io", *attachment_image.IMPORT_ARGUMENTS, "/tmp/attachment-copy.tar")
    tag = f"{attachment_image.IMAGE_NAME}:sha-{source}"
    listing = images()
    tagged = next((line for line in listing.splitlines() if line.startswith(tag + " ")), "")
    created = pinned in listing
    if digest not in tagged:
        raise RuntimeError("the imported name does not hold the built digest")
    if not created:
        docker("exec", K3S_CONTAINER, "ctr", "-n", "k8s.io", "images", "tag", tag, pinned)
    return pinned, {"digest_name_created_by_import": created}


def start_source(directory):
    """MinIO on port 443 as the real S3 host name, so the SDK resolves the endpoint itself."""
    root = (secrets.token_hex(10), secrets.token_hex(20))
    certificates = Path(directory, "s3-certs")
    (certificates / "CAs").mkdir(parents=True)
    (certificates / "public.crt").write_bytes(Path(directory, "s3.crt").read_bytes())
    # Readable by the disposable container's user; this stand-in key leaves with the container.
    (certificates / "private.key").write_bytes(Path(directory, "s3.key").read_bytes())
    os.chmod(certificates / "private.key", 0o644)
    docker("create", "--name", guarded(S3_CONTAINER), "--network", NETWORK, "--ip", S3_IP, "-p", "127.0.0.1::443",
           "-e", "MINIO_ROOT_USER=" + root[0], "-e", "MINIO_ROOT_PASSWORD=" + root[1],
           "-e", "MINIO_DOMAIN=" + S3_HOST, "-e", "MINIO_REGION=" + REGION,
           "--tmpfs", "/data:uid=65532,gid=65532", MINIO_IMAGE, "server", "/data", "--address", ":443",
           "--certs-dir", "/certs")
    docker("cp", str(certificates), S3_CONTAINER + ":/certs")
    docker("start", S3_CONTAINER)
    port = docker("port", S3_CONTAINER, "443/tcp").stdout.splitlines()[0].rsplit(":", 1)[1]
    return {"endpoint": f"https://127.0.0.1:{port}", "root": root, "ca": str(Path(directory, "ca.crt"))}


def source_client(source, credentials=None, service="s3"):
    import boto3
    from botocore.config import Config
    key, secret, token = credentials or (*source["root"], None)
    return boto3.client(service, endpoint_url=source["endpoint"], region_name=REGION, aws_access_key_id=key,
                        aws_secret_access_key=secret, aws_session_token=token, verify=source["ca"],
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                                      request_checksum_calculation="when_required",
                                      response_checksum_validation="when_required"))


def source_state(source):
    client = source_client(source)
    return {item["Key"]: (item["ETag"], item["LastModified"].isoformat())
            for item in client.list_objects_v2(Bucket=SOURCE_BUCKET).get("Contents", ())}


def source_session(source):
    """Same response shape as `aws sts get-federation-token`, narrowed by a session policy."""
    # Only the rendered S3 statements with an Action: MinIO does not model the KMS allow, and the
    # NotAction deny names that KMS action. So this does not evaluate the deny of other actions.
    policy = attachment_job.source_session_policy(SOURCE_BUCKET_ARN, SOURCE_KMS_KEY_ARN)
    policy["Statement"] = [s for s in policy["Statement"] if "s3:" in json.dumps(s.get("Action", ""))]
    response = source_client(source, service="sts").assume_role(
        RoleArn="arn:minio:iam:::role/t8-job", RoleSessionName="t8-attachment-copy",
        DurationSeconds=attachment_job.SOURCE_SESSION_SECONDS, Policy=json.dumps(policy))
    return response["Credentials"]


def start_database(directory):
    password = secrets.token_hex(20)
    certificates = Path(directory, "db-certs")
    certificates.mkdir()
    for name in ("db.crt", "db.key"):
        (certificates / name).write_bytes(Path(directory, name).read_bytes())
    prepare = ("install -o postgres -m 600 /certs/db.key /tmp/db.key && install -o postgres -m 644 /certs/db.crt "
               "/tmp/db.crt && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/db.crt "
               "-c ssl_key_file=/tmp/db.key -c log_connections=on")
    docker("create", "--name", guarded(DB_CONTAINER), "--network", NETWORK, "--ip", DB_IP,
           "-e", "POSTGRES_PASSWORD=" + secrets.token_hex(20), "-e", "POSTGRES_DB=chat",
           "--entrypoint", "sh", DB_IMAGE, "-c", prepare)
    docker("cp", str(certificates), DB_CONTAINER + ":/certs")
    docker("start", DB_CONTAINER)

    def psql(sql):
        return docker("exec", "-i", DB_CONTAINER, "psql", "-U", "postgres", "-d", "chat", "-v", "ON_ERROR_STOP=1",
                      "-At", stdin=sql, check_exit=False)

    def ready():
        probe = docker("exec", DB_CONTAINER, "pg_isready", "-U", "postgres", "-d", "chat", "-h", "127.0.0.1",
                       check_exit=False)
        return probe.returncode == 0

    wait_for(ready, 120, "database")
    schema = (REPO / "infra/envs/source-workloads/schema.sql").read_text()
    # Roles come from render.py's postInitSQL on the real source; the grants are the repository's own.
    created = psql(f"CREATE ROLE chat_app LOGIN; CREATE ROLE chat_replicator LOGIN REPLICATION PASSWORD '{password}';"
                   + schema)
    if created.returncode:
        raise RuntimeError("schema failed: " + created.stderr[-400:])
    return {"password": password, "psql": psql}


def add_rows(database, keys):
    values = ",".join(f"(gen_random_uuid(),'{key}','file','application/octet-stream',1)" for key in keys)
    inserted = database["psql"](f"INSERT INTO attachments (id,object_key,filename,content_type,size) VALUES {values};")
    if inserted.returncode:
        raise RuntimeError("row insert failed: " + inserted.stderr[-400:])


class Cluster:
    def __init__(self, directory):
        self.config = str(Path(directory, "kubeconfig"))
        self.environment = {**os.environ, "KUBECONFIG": self.config}

    def kubectl(self, *arguments, stdin=None, check_exit=True, timeout=180):
        return run("kubectl", "--kubeconfig", self.config, *arguments, stdin=stdin, check_exit=check_exit,
                   timeout=timeout)

    def ns(self, *arguments, **options):
        return self.kubectl("-n", NS, *arguments, **options)

    def apply(self, *items):
        self.kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(items)}))

    def get(self, *arguments):
        return json.loads(self.ns("get", *arguments, "-o", "json").stdout)

    def secret_from_env(self, name, values):
        lines = "".join(f"{key}={value}\n" for key, value in values.items())
        self.ns("create", "secret", "generic", name, "--from-env-file=/dev/stdin", stdin=lines)

    def secret_from_file(self, name, key, content):
        self.ns("create", "secret", "generic", name, f"--from-file={key}=/dev/stdin", stdin=content)


def start_k3s(directory):
    registries = REPO / "services/exit-drill/tests/integration/k3s-registries.yaml"
    docker("run", "-d", "--name", guarded(K3S_CONTAINER), "--network", NETWORK, "--ip", K3S_IP, "--privileged",
           "--tmpfs", "/run", "--tmpfs", "/var/run", "-p", "127.0.0.1::6443",
           "-v", f"{registries}:/etc/rancher/k3s/registries.yaml:ro", K3S_IMAGE, "server",
           "--disable", "traefik", "--disable", "servicelb", "--disable", "metrics-server",
           "--disable", "local-storage", "--tls-san", "127.0.0.1")
    cluster = Cluster(directory)

    def write_config():
        text = docker("exec", K3S_CONTAINER, "cat", "/etc/rancher/k3s/k3s.yaml").stdout
        port = docker("port", K3S_CONTAINER, "6443/tcp").stdout.splitlines()[0].rsplit(":", 1)[1]
        Path(cluster.config).write_text(text.replace("https://127.0.0.1:6443", f"https://127.0.0.1:{port}"))
        os.chmod(cluster.config, 0o600)
        return True

    wait_for(write_config, 120, "kubeconfig")
    wait_for(lambda: " Ready" in cluster.kubectl("get", "nodes", "--no-headers", check_exit=False).stdout, 240, "node")
    wait_for(lambda: "1/1" in cluster.kubectl("-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-dns",
                                              "--no-headers", check_exit=False).stdout, 300, "cluster DNS")
    return cluster


def fetch_object_store_image():
    """The exact digest objects.py names, from a public mirror, so the alias below cannot name other bytes."""
    mirrored = "mirror.gcr.io/chrislusf/seaweedfs:4.52@" + object_store.IMAGE.split("@")[1]
    docker("pull", "--platform", "linux/amd64", mirrored, timeout=600)
    docker("tag", mirrored, STORE_REFERENCE)


def import_image(reference, platform):
    saved = subprocess.Popen(["docker", "save", "--platform", "linux/" + platform, reference], stdout=subprocess.PIPE)
    subprocess.run(["docker", "exec", "-i", K3S_CONTAINER, "ctr", "-n", "k8s.io", "images", "import",
                    "--platform", "linux/" + platform, "-"], stdin=saved.stdout, check=True, capture_output=True,
                   timeout=600)
    saved.stdout.close()
    if saved.wait(timeout=60):
        raise RuntimeError("docker save failed for " + reference)


def pinned_reference(tag):
    """Name the imported bytes by their descriptor digest, as the image handoff guide requires."""
    name = "docker.io/library/" + tag
    listing = docker("exec", K3S_CONTAINER, "ctr", "-n", "k8s.io", "images", "ls", "name==" + name).stdout
    digest = re.search(r"sha256:[0-9a-f]{64}", listing).group(0)
    pinned = f"docker.io/library/{tag.split(':')[0]}@{digest}"
    docker("exec", K3S_CONTAINER, "ctr", "-n", "k8s.io", "images", "tag", name, pinned)
    return pinned


def deploy_object_store(cluster, node):
    """objects.py's own Deployment, PV, Service and NetworkPolicy with the two documented identities."""
    bootstrap = {"K": secrets.token_hex(10), "S": secrets.token_hex(20)}
    client = {"AWS_ACCESS_KEY_ID": secrets.token_hex(10), "AWS_SECRET_ACCESS_KEY": secrets.token_hex(20)}

    auth = attachment_containers.auth_config((bootstrap["K"], bootstrap["S"]),
                                             (client["AWS_ACCESS_KEY_ID"], client["AWS_SECRET_ACCESS_KEY"]))
    cluster.secret_from_file("target-seaweedfs-auth", "s3.json", auth)
    cluster.secret_from_env("target-object-client", client)
    cluster.secret_from_env("t8-job-bootstrap", bootstrap)
    docker("exec", K3S_CONTAINER, "install", "-d", "-o", "1000", "-g", "1000", "/var/lib/bravo-target-dev/objects")
    docker("exec", K3S_CONTAINER, "ctr", "-n", "k8s.io", "images", "tag", STORE_REFERENCE,
           "docker.io/chrislusf/seaweedfs@" + object_store.IMAGE.split("@")[1])
    cluster.apply(*object_store.objects(node))
    cluster.ns("rollout", "status", "deployment/target-seaweedfs", "--timeout=420s", timeout=450)
    return client, bootstrap


S3_HELPER = """
import hashlib, json, os, sys, time
import boto3
from botocore.config import Config
client = boto3.client("s3", endpoint_url="{endpoint}", region_name="us-east-1", aws_access_key_id=os.environ[{key!r}],
    aws_secret_access_key=os.environ[{secret!r}], aws_session_token=None,
    config=Config(signature_version="s3v4", s3={{"addressing_style": "path"}},
                  request_checksum_calculation="when_required", response_checksum_validation="when_required"))
bucket = "{bucket}"
"""
BOOTSTRAP_SCRIPT = """
for _ in range(120):
    try:
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            client.create_bucket(Bucket=bucket)
        client.put_object(Bucket=bucket, Key="readiness", Body=b"ready")
        client.delete_object(Bucket=bucket, Key="readiness")
        print("ready")
        sys.exit(0)
    except Exception:
        time.sleep(2)
sys.exit(1)
"""
LIST_SCRIPT = """
found = {}
for item in client.list_objects_v2(Bucket=bucket).get("Contents", ()):
    body = client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
    found[hashlib.sha256(item["Key"].encode()).hexdigest()] = hashlib.sha256(body).hexdigest()
print(json.dumps(found))
"""


ROUTE_PROBE = f"""
import json, socket, time
started = time.monotonic()
errors = []
while time.monotonic() - started < 60:
    try:
        socket.create_connection(({attachment_job.TARGET_HOST!r}, {attachment_job.TARGET_PORT}), timeout=2).close()
        print(json.dumps({{"errors": errors, "seconds_until_open": round(time.monotonic() - started, 2)}}))
        break
    except OSError as error:
        errors.append(type(error).__name__)
        time.sleep(0.1)
"""
# One attempt only, after a delay: a retry loop cannot tell elapsed time from the number of attempts.
ONE_ATTEMPT_PROBE = f"""
import json, socket, time
time.sleep(DELAY)
try:
    socket.create_connection(({attachment_job.TARGET_HOST!r}, {attachment_job.TARGET_PORT}), timeout=2).close()
    print(json.dumps({{"delay_seconds": DELAY, "opened": True}}))
except OSError as error:
    print(json.dumps({{"delay_seconds": DELAY, "opened": False, "error": type(error).__name__}}))
"""
# The copy code's own destination client, from the first moment of a Pod.
SDK_PROBE = f"""
import json, os, sys, time
sys.path.insert(0, "/app")
started = time.monotonic()
import attachment_stores
store = attachment_stores.destination_store({attachment_job.TARGET_ENDPOINT!r}, {attachment_job.TARGET_BUCKET!r},
                                            os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"])
ready = round(time.monotonic() - started, 2)
attempts = []
while time.monotonic() - started < 60:
    try:
        store.list_keys()
        attempts.append([round(time.monotonic() - started, 2), "ok"])
        break
    except Exception as error:
        attempts.append([round(time.monotonic() - started, 2), type(error).__name__])
        time.sleep(0.2)
print(json.dumps({{"client_ready_seconds": ready, "attempts": attempts}}))
"""


def in_cluster(cluster, image, name, secret, key, secret_key, script):
    """Read or prepare the object store with an SDK that retries, for bytes the receipt does not vouch for."""
    code = S3_HELPER.format(endpoint=attachment_job.TARGET_ENDPOINT, key=key, secret=secret_key,
                            bucket=attachment_job.TARGET_BUCKET) + script
    return short_pod(cluster, image, name, code, secret)


def short_pod(cluster, image, name, code, secret=None):
    """A short restricted Pod with the label the object store admits."""
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": name, "namespace": NS, "labels": {"bravo-object-client": "true"}},
           "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{"name": "helper", "image": image, "imagePullPolicy": "IfNotPresent",
                                    "command": ["python3", "-c", code],
                                    "envFrom": [{"secretRef": {"name": secret}}] if secret else [],
                                    "securityContext": PYTHON_SECURITY}]}}
    cluster.apply(pod)

    def finished():
        phase = cluster.get("pod", name)["status"].get("phase")
        return phase if phase in ("Succeeded", "Failed") else None

    phase = wait_for(finished, 330, name)
    output = cluster.ns("logs", "pod/" + name).stdout
    cluster.ns("delete", "pod", name, "--wait=true")
    if phase != "Succeeded":
        raise RuntimeError(name + " failed")
    return output


def recipe_from_guide():
    """The operator's Secret pipeline, minus its AWS first command. A changed guide must still work."""
    block = re.search(r"<!-- source-secret-recipe -->\s*```sh\n(.*?)```", GUIDE.read_text(), re.DOTALL).group(1)
    lines = block.splitlines()
    start = next(index for index, line in enumerate(lines) if line.lstrip().startswith("| jq"))
    return "cat \\\n" + "\n".join(lines[start:])


def create_source_secret(cluster, session):
    payload = json.dumps({"Credentials": session}, default=str)
    done = run("bash", "-euo", "pipefail", "-c", recipe_from_guide(), stdin=payload, check_exit=False,
               env={**cluster.environment, "SOURCE_BUCKET": SOURCE_BUCKET, "SOURCE_REGION": REGION,
                    "SOURCE_BUCKET_OWNER": BUCKET_OWNER})
    if done.returncode:
        raise RuntimeError("source Secret recipe failed: " + done.stderr[-400:])


class Harness:
    def __init__(self, cluster, source, database, images, route):
        self.cluster = cluster
        self.source = source
        self.database = database
        self.images = images
        self.route = route
        self.collected = []

    def rendered(self, image, run_id, **options):
        return attachment_job.job(image, run_id, PLAN_ID, EPOCH, self.route["address"], self.route["uid"], **options)

    def with_stand_in(self, job):
        """Point the unchanged S3 host name at MinIO. The environment stays exactly as rendered."""
        pod = job["spec"]["template"]["spec"]
        pod["hostAliases"] = [{"ip": S3_IP, "hostnames": [S3_HOST, f"{SOURCE_BUCKET}.{S3_HOST}"]}]
        pod["volumes"].append({"name": "stand-in-ca", "secret": {"secretName": "t8-job-stand-in-ca"}})
        pod["containers"][0]["volumeMounts"].append(
            {"name": "stand-in-ca", "mountPath": BOTOCORE_BUNDLE, "subPath": "ca.crt", "readOnly": True})
        return job

    def start(self, job):
        self.cluster.ns("create", "-f", "-", stdin=json.dumps(job))

    def wait(self, run_id, seconds=420):
        name = attachment_job.job_name(run_id)

        def finished():
            conditions = self.cluster.get("job", name)["status"].get("conditions", [])
            return any(c["type"] in ("Complete", "Failed") and c["status"] == "True" for c in conditions)

        wait_for(finished, seconds, name)

    def verdict(self, run_id, image, plan_id=PLAN_ID):
        done = run(sys.executable, str(WORKLOADS / "attachment_verdict.py"), "--run-id", run_id, "--plan-id", plan_id,
                   "--epoch", str(EPOCH), "--image", image, "--kubeconfig", self.cluster.config,
                   check_exit=False, timeout=120)
        self.collected.append(done.stdout + done.stderr)
        return done.returncode, json.loads(done.stdout)

    def collect_log(self, run_id):
        selector = "batch.kubernetes.io/job-name=" + attachment_job.job_name(run_id)
        logs = self.cluster.ns("logs", "-l", selector, "--tail=-1", check_exit=False).stdout
        self.collected.append(logs)
        return logs

    def run_to_verdict(self, run_id, image=None, job=None, **options):
        image = image or self.images["copy"]
        self.start(job or self.with_stand_in(self.rendered(image, run_id, **options)))
        self.wait(run_id)
        self.collect_log(run_id)
        return self.verdict(run_id, image)

    def destination(self):
        output = in_cluster(self.cluster, self.images["copy"], "t8-job-list-" + secrets.token_hex(3),
                            "target-object-client", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", LIST_SCRIPT)
        return json.loads(output)


def digest_map(objects):
    return {hashlib.sha256(key.encode()).hexdigest(): hashlib.sha256(body).hexdigest() for key, body in objects.items()}


def scenario_admission(harness):
    job = harness.rendered(harness.images["copy"], "admission")
    template = job["spec"]["template"]

    def pod_from_template(mutate=None):
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "t8-job-admission", "namespace": NS},
               "spec": json.loads(json.dumps(template["spec"]))}
        if mutate:
            mutate(pod["spec"])
        return harness.cluster.ns("create", "--dry-run=server", "-f", "-", stdin=json.dumps(pod), check_exit=False)

    check("restricted_namespace_admits_the_rendered_pod", pod_from_template().returncode == 0)

    def privileged(spec):
        spec["containers"][0]["securityContext"]["allowPrivilegeEscalation"] = True

    denied = pod_from_template(privileged)
    check("restricted_namespace_is_enforcing_control", denied.returncode != 0 and "PodSecurity" in denied.stderr)


def scenario_read_only_session(harness, session):
    from botocore.exceptions import ClientError
    credentials = (session["AccessKeyId"], session["SecretAccessKey"], session["SessionToken"])
    client = source_client(harness.source, credentials)
    listed = client.list_objects_v2(Bucket=SOURCE_BUCKET).get("KeyCount", 0)
    body = client.get_object(Bucket=SOURCE_BUCKET, Key="attachments/plain.txt")["Body"].read()
    check("session_reads_the_source_bucket", listed == len(OBJECTS) and body == OBJECTS["attachments/plain.txt"])

    def denied(operation):
        try:
            operation()
        except ClientError as error:
            return error.response["Error"]["Code"] == "AccessDenied"
        return False

    check("session_cannot_write_or_delete_the_source", all([
        denied(lambda: client.put_object(Bucket=SOURCE_BUCKET, Key="attachments/plain.txt", Body=b"overwritten")),
        denied(lambda: client.delete_object(Bucket=SOURCE_BUCKET, Key="attachments/plain.txt")),
    ]))
    source_client(harness.source).create_bucket(Bucket="t8-job-other-bucket")
    check("session_cannot_read_another_bucket", denied(lambda: client.list_objects_v2(Bucket="t8-job-other-bucket")))


def scenario_copy_and_rerun(harness):
    expected = digest_map(OBJECTS)
    before = source_state(harness.source)
    code, first = harness.run_to_verdict("first")
    receipt = first.get("receipt") or {}
    check("first_run_succeeds_and_copies_every_object",
          code == 0 and first["verdict"] == "SUCCEEDED" and (receipt.get("copied"), receipt.get("reused")) == (3, 0),
          exit_code=code, verdict=first["verdict"], reason=first["reason_code"], copied=receipt.get("copied"),
          reused=receipt.get("reused"), bytes_verified=receipt.get("bytes_verified"),
          failure=receipt.get("reason"), stage=receipt.get("stage"), error_category=receipt.get("error_category"),
          s3_error_code=receipt.get("s3_error_code"))
    check("result_is_bound_to_the_run", first["run_id"] == "first" and first["plan_id"] == PLAN_ID
          and first["epoch"] == EPOCH and first["job_uid"] and first["pod_uid"]
          and first["started_at"] and first["finished_at"] and first["image_digest"] in harness.images["copy"])
    check("destination_bytes_match_independently_of_the_receipt", harness.destination() == expected,
          destination_count=len(expected))
    check("source_objects_untouched", source_state(harness.source) == before)

    code, second = harness.run_to_verdict("second")
    again = second.get("receipt") or {}
    check("rerun_reuses_every_object_with_the_same_inventory_hash",
          code == 0 and (again.get("copied"), again.get("reused")) == (0, 3)
          and again.get("key_content_inventory_sha256") == receipt.get("key_content_inventory_sha256"),
          copied=again.get("copied"), reused=again.get("reused"))
    return first, second


def observe_route_opening(harness):
    """Why the wait exists: how soon a new labeled Pod can reach the object store, and what happens without it."""
    image = harness.images["copy"]
    probes = [json.loads(short_pod(harness.cluster, image, f"t8-job-route-{index}", ROUTE_PROBE)) for index in range(3)]
    one_attempt = [json.loads(short_pod(harness.cluster, image, f"t8-job-once-{index}",
                                        ONE_ATTEMPT_PROBE.replace("DELAY", str(delay))))
                   for index, delay in enumerate((0, 0.5, 1, 2, 4, 8))]
    sdk = [json.loads(short_pod(harness.cluster, image, f"t8-job-sdk-{index}", SDK_PROBE, "target-object-client"))
           for index in range(3)]
    job = harness.with_stand_in(harness.rendered(harness.images["copy"], "no-wait"))
    del job["spec"]["template"]["spec"]["initContainers"]
    code, result = harness.run_to_verdict("no-wait", job=job)
    receipt = result.get("receipt") or {}
    return {"new_pod_reaching_the_object_store": probes, "one_attempt_after_a_delay": one_attempt,
            "copy_destination_client_from_pod_start": sdk,
            "copy_without_the_wait": {"exit_code": code, "verdict": result["verdict"],
                                      "error_category": receipt.get("error_category")}}


def scenario_reported_failure(harness):
    before = harness.destination()
    source_client(harness.source).put_object(Bucket=SOURCE_BUCKET, Key=EXTRA_KEY, Body=EXTRA_BODY)
    code, result = harness.run_to_verdict("unexplained")
    check("unexplained_source_object_is_failed_and_writes_nothing",
          code == 1 and result["verdict"] == "FAILED"
          and result["receipt"]["reason"] == "source bucket and database attachment inventory differ"
          and harness.destination() == before,
          exit_code=code, verdict=result["verdict"], reason=result["receipt"] and result["receipt"]["reason"])


def scenario_approved_extras(harness):
    harness.cluster.secret_from_file(attachment_job.EXTRAS_SECRET, "keys", EXTRA_KEY + "\n")
    code, result = harness.run_to_verdict("extras", with_approved_extras=True)
    receipt = result.get("receipt") or {}
    check("explicitly_approved_object_is_copied_and_counted_separately",
          code == 0 and result["verdict"] == "SUCCEEDED" and receipt.get("operator_approved_extra_count") == 1
          and receipt.get("database_referenced_count") == 3 and receipt.get("copied") == 1,
          exit_code=code, verdict=result["verdict"], extras=receipt.get("operator_approved_extra_count"),
          database=receipt.get("database_referenced_count"), copied=receipt.get("copied"))
    expected = digest_map({**OBJECTS, EXTRA_KEY: EXTRA_BODY})
    check("object_counts_are_equal_on_both_sides", harness.destination() == expected, count=len(expected))


def scenario_network_policy(harness):
    job = harness.with_stand_in(harness.rendered(harness.images["copy"], "unlabeled"))
    del job["spec"]["template"]["metadata"]["labels"]["bravo-object-client"]
    code, result = harness.run_to_verdict("unlabeled", job=job)
    receipt = result.get("receipt") or {}
    # The wait never succeeds, so the copy never starts: no receipt and nothing written.
    check("pod_without_the_label_cannot_reach_the_object_store_control",
          code == 2 and result["verdict"] == "UNKNOWN" and result["reason_code"] == "pod_not_terminated"
          and not receipt, exit_code=code, verdict=result["verdict"], reason=result["reason_code"])


def scenario_crash_without_receipt(harness):
    code, result = harness.run_to_verdict("crash", image=harness.images["broken"])
    logs = harness.collect_log("crash")
    check("crash_before_a_receipt_is_unknown_with_an_empty_log",
          code == 2 and result["verdict"] == "UNKNOWN" and result["reason_code"] == "receipt_unreadable"
          and result["container_exit_code"] == 1 and logs.strip() == "",
          exit_code=code, verdict=result["verdict"], reason=result["reason_code"], log_bytes=len(logs))


def scenario_session_token_required(harness, session):
    harness.cluster.ns("delete", "secret", attachment_job.SOURCE_SECRET)
    harness.cluster.secret_from_env(attachment_job.SOURCE_SECRET, {
        "bucket": SOURCE_BUCKET, "region": REGION, "bucket_owner": BUCKET_OWNER,
        "aws_access_key_id": session["AccessKeyId"], "aws_secret_access_key": session["SecretAccessKey"]})
    job = harness.with_stand_in(harness.rendered(harness.images["copy"], "no-token"))
    job["spec"]["activeDeadlineSeconds"] = 40
    harness.start(job)

    def blocked():
        pods = harness.cluster.get("pods", "-l", "batch.kubernetes.io/job-name=attachment-copy-no-token")["items"]
        statuses = pods[0]["status"].get("containerStatuses", []) if pods else []
        return bool(statuses) and statuses[0]["state"].get("waiting", {}).get("reason") == "CreateContainerConfigError"

    wait_for(blocked, 60, "missing session token to block the container")
    check("long_lived_key_without_a_session_token_never_starts", True)
    harness.wait("no-token", 180)
    code, result = harness.verdict("no-token", harness.images["copy"])
    check("deadline_without_a_receipt_is_unknown_and_keeps_the_cause",
          code == 2 and result["verdict"] == "UNKNOWN" and result["job_failure_reason"] == "DeadlineExceeded",
          exit_code=code, reason=result["reason_code"], job_failure_reason=result["job_failure_reason"])


def scenario_unreadable_result(harness):
    image = harness.images["copy"]
    selector = "batch.kubernetes.io/job-name=attachment-copy-first"
    harness.cluster.ns("delete", "pods", "-l", selector, "--wait=true")
    code, result = harness.verdict("first", image)
    check("success_is_not_counted_once_the_pod_is_gone",
          code == 2 and result["verdict"] == "UNKNOWN" and result["reason_code"] == "pod_count_not_one",
          exit_code=code, reason=result["reason_code"])
    code, result = harness.verdict("never-started", image)
    check("a_job_that_does_not_exist_is_unknown",
          code == 2 and result["verdict"] == "UNKNOWN" and result["reason_code"] == "job_unreadable",
          exit_code=code, reason=result["reason_code"])
    code, result = harness.verdict("second", image.replace(image.split("@")[1], "sha256:" + "0" * 64))
    check("another_image_digest_is_not_this_run", code == 2 and result["reason_code"] == "job_not_as_rendered",
          exit_code=code, reason=result["reason_code"])
    code, result = harness.verdict("second", image, plan_id="Another_Plan")
    check("another_plan_cannot_use_this_result", code == 2 and result["reason_code"] == "job_not_as_rendered",
          exit_code=code, reason=result["reason_code"])


def scenario_database_boundary(harness, database):
    connections = docker("logs", DB_CONTAINER).stderr
    authorized = [line for line in connections.splitlines()
                  if "connection authorized: user=chat_replicator" in line and "bravo_t8_attachment_copy" in line]
    check("job_connected_as_the_replication_role_over_tls",
          bool(authorized) and all("SSL enabled" in line for line in authorized), connections=len(authorized))
    denied = docker("exec", "-i", DB_CONTAINER, "psql", "-U", "chat_replicator", "-d", "chat", "-v", "ON_ERROR_STOP=1",
                    stdin="DELETE FROM attachments;", check_exit=False)
    check("replication_role_cannot_change_attachment_rows",
          denied.returncode != 0 and "permission denied" in denied.stderr)


def scenario_no_leak(harness, session, client, bootstrap, database):
    values = {
        "session_key": session["AccessKeyId"], "session_secret": session["SecretAccessKey"],
        "session_token": session["SessionToken"], "database_password": database["password"],
        "target_key": client["AWS_ACCESS_KEY_ID"], "target_secret": client["AWS_SECRET_ACCESS_KEY"],
        "bootstrap_key": bootstrap["K"], "source_bucket": SOURCE_BUCKET, "bucket_owner": BUCKET_OWNER,
        "s3_host": S3_HOST, "database_host": "source-db-rw", "extra_key": EXTRA_KEY,
        **{f"object_key_{index}": key for index, key in enumerate(OBJECTS)},
    }
    text = "\n".join(harness.collected)
    leaked = sorted(name for name, value in values.items() if value in text)
    check("logs_and_verdicts_carry_no_secret_key_or_private_name", not leaked and len(text) > 0,
          leaked=leaked, scanned_bytes=len(text))
    manifest = json.dumps(harness.rendered(harness.images["copy"], "scan"))
    credential_names = ("session_key", "session_secret", "session_token", "database_password", "target_key",
                        "target_secret", "source_bucket", "bucket_owner")
    in_manifest = sorted(name for name in credential_names if values[name] in manifest)
    check("rendered_manifest_carries_only_secret_names", not in_manifest, leaked=in_manifest)
    stored = harness.cluster.get("job", "attachment-copy-second")
    check("stored_job_holds_no_credential", not [name for name in credential_names[:6] if values[name] in json.dumps(stored)])


def image_records():
    def digest_of(reference):
        return docker("image", "inspect", reference, "--format", "{{.Id}}").stdout.strip()

    return {"k3s": K3S_IMAGE, "k3s_digest": digest_of(K3S_IMAGE), "minio": MINIO_IMAGE, "database": DB_IMAGE,
            "database_digest": digest_of(DB_IMAGE), "object_store": object_store.IMAGE,
            "object_store_platform": "linux/amd64 emulated on arm64"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    started = time.time()
    cleanup()
    try:
        with tempfile.TemporaryDirectory(prefix="t8-job-") as directory:
            os.chmod(directory, 0o700)
            issue_certificates(directory)
            docker("network", "create", "--subnet", SUBNET, guarded(NETWORK))
            build_images(directory)
            copy_archive, copy_digest, copy_source = build_copy_archive(directory)
            fetch_object_store_image()
            source = start_source(directory)
            wait_for(lambda: source_client(source).list_buckets() is not None, 120, "stand-in S3")
            source_client(source).create_bucket(Bucket=SOURCE_BUCKET)
            for key, body in OBJECTS.items():
                source_client(source).put_object(Bucket=SOURCE_BUCKET, Key=key, Body=body)
            database = start_database(directory)
            add_rows(database, OBJECTS)

            cluster = start_k3s(directory)
            node = cluster.kubectl("get", "nodes", "-o", "jsonpath={.items[0].metadata.name}").stdout
            namespace, storage = render.database(node)[:2]
            cluster.apply(namespace, storage)
            import_image(STORE_REFERENCE, "amd64")
            architecture = docker("image", "inspect", COPY_TAG, "--format", "{{.Architecture}}").stdout.strip()
            import_image(BROKEN_TAG, architecture)
            copy_reference, import_observation = import_copy_archive(copy_archive, copy_digest, copy_source)
            images = {"copy": copy_reference, "broken": pinned_reference(BROKEN_TAG)}

            client, bootstrap = deploy_object_store(cluster, node)
            in_cluster(cluster, images["copy"], "t8-job-bootstrap", "t8-job-bootstrap", "K", "S", BOOTSTRAP_SCRIPT)

            # The route and Secrets the operator confirms before rendering.
            cluster.apply(
                {"apiVersion": "v1", "kind": "Service",
                 "metadata": {"name": "source-db-for-replication", "namespace": NS},
                 "spec": {"ports": [{"name": "postgres", "port": 5432, "protocol": "TCP"}]}},
                {"apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
                 "metadata": {"name": "source-db-for-replication-1", "namespace": NS,
                              "labels": {"kubernetes.io/service-name": "source-db-for-replication"}},
                 "addressType": "IPv4", "ports": [{"name": "postgres", "port": 5432, "protocol": "TCP"}],
                 "endpoints": [{"addresses": [DB_IP]}]})
            service = cluster.get("service", "source-db-for-replication")
            route = {"address": service["spec"]["clusterIP"], "uid": service["metadata"]["uid"]}
            cluster.secret_from_env("t8-source-replication", {"password": database["password"]})
            ca = Path(directory, "ca.crt").read_text()
            cluster.secret_from_file("t8-source-ca", "ca.crt", ca)
            cluster.secret_from_file("t8-job-stand-in-ca", "ca.crt", ca)
            session = source_session(source)
            create_source_secret(cluster, session)

            harness = Harness(cluster, source, database, images, route)
            scenario_admission(harness)
            scenario_read_only_session(harness, session)
            first, second = scenario_copy_and_rerun(harness)
            route_opening = observe_route_opening(harness)
            scenario_reported_failure(harness)
            scenario_approved_extras(harness)
            scenario_network_policy(harness)
            scenario_crash_without_receipt(harness)
            scenario_session_token_required(harness, session)
            scenario_unreadable_result(harness)
            scenario_database_boundary(harness, database)
            scenario_no_leak(harness, session, client, bootstrap, database)
            sample = {"copied_run": {key: value for key, value in first.items() if key not in ("job_uid", "pod_uid")},
                      "rerun_counts": {k: (second["receipt"] or {}).get(k) for k in ("copied", "reused", "source_count")}}
    finally:
        cleanup()
    failed = [item["name"] for item in checks if not item["passed"]]
    args.out.write_text(json.dumps({
        "schema_version": 1,
        "artifact_kind": "disposable k3s run of the rendered attachment copy Job; no cloud resource was called",
        "stand_ins": {"source_s3": "MinIO as s3.<region>.amazonaws.com through hostAliases and a test CA",
                      "sts_and_iam": "MinIO STS session policy: the rendered S3 allows and the bucket deny, without the KMS allow and the NotAction deny",
                      "egress_route": "selector-less Service to a PostgreSQL container",
                      "cluster": "k3s, not RKE2"},
        "images": image_records(), "python": sys.version.split()[0],
        "checks_total": len(checks), "checks_passed": len(checks) - len(failed), "checks_failed": failed,
        "checks": checks, "observations": {**route_opening, "copy_image_import": import_observation}, "sample": sample,
        "seconds": round(time.time() - started),
    }, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed in {round(time.time() - started)}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
