"""The source for the isolated E2E: the running app the migration starts from, inside the same cluster.

What is the team's own definition, unchanged: the source schema and publication (source-workloads/
schema.sql), the source pooler (source-workloads/render.py `pooler`), PostgreSQL 18.4 from the same image,
the app image the destination deploys, and the app's own settings.
What stands in, and for what:
  postgres Deployment `source-db`  for the CNPG Cluster (same image, TLS and pg_hba as its spec; the
                                     Services `source-db-rw` and the CA/server Secrets CNPG would create)
  moto in namespace `aws`          for AWS Secrets Manager (the app's DB credential)
  SeaweedFS `source-objects`       for S3 (attachments): the design's S3-compatible store, same image and
                                     server flags as the destination, bucket policy from the T9 renderer
                                     (closed: only the app identity reads and writes, like the S3 bucket)
  plain Secrets                    for ESO/IRSA (static moto credentials stand in for the IAM role)
The CA stays in the platform vault (`source-ca`): the destination's replication verifies the source
server with it (verify-full), as it verifies the real AWS source.

usage: python source_fixture.py up <destination.json>     create or confirm; prints nothing secret
       python source_fixture.py seed <destination.json> N  write N messages through the source app
"""

import base64
import importlib.util
import ipaddress
import json
import secrets
import string
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from bravo_migration.host import LocalVmHost
from bravo_migration.kube import Kube
from bravo_migration.vault import PlatformVault

NS, AWS_NS = "source-dev", "aws"
APP_IMAGE = "localhost/bravo-demo-chat@sha256:b351ba11819796aaea854f742049e46b058561f80c0e738f42c93280af762888"
APP_NAMES = ["localhost/bravo-demo-chat:8d82a924-arm64", APP_IMAGE]
MOTO_IMAGE = "motoserver/moto:5.1.4"
REGION = "ap-northeast-2"
SECRET_ID = "demo-chat/db"
BUCKET = "demo-chat-attachments"
# Names the server certificate answers to: its own Services and the destination's kube DNS name for it.
DB_NAMES = ["source-db-rw", f"source-db-rw.{NS}", f"source-db-rw.{NS}.svc", f"source-db-rw.{NS}.svc.cluster.local",
            "source-pgbouncer", f"source-pgbouncer.{NS}", f"source-pgbouncer.{NS}.svc",
            f"source-pgbouncer.{NS}.svc.cluster.local",
            "source-db-for-replication.target-dev.svc", "source-db-for-replication.target-dev.svc.cluster.local"]
RESTRICTED = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
POD_SECURITY = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}


def _random(n):
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def _module(repo, name):
    spec = importlib.util.spec_from_file_location(name, repo / "infra/envs/source-workloads/render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tls():
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bravo e2e source-db CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "source-db-rw")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([*map(x509.DNSName, DB_NAMES),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    return {"ca_crt": ca.public_bytes(pem).decode(), "tls_crt": leaf.public_bytes(pem).decode(),
            "tls_key": key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}


def secret(name, namespace, data, kind="Opaque"):
    return {"apiVersion": "v1", "kind": "Secret", "type": kind, "metadata": {"name": name, "namespace": namespace},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}


def namespace(name):
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": {
        "pod-security.kubernetes.io/enforce": "restricted", "bravo-e2e/role": "source"}}}


def database(image):
    """PostgreSQL 18.4 with the CNPG Cluster's TLS, parameters and pg_hba (source-workloads/render.py)."""
    start = """set -eu
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  # CNPG's initdb defaults: UTF8, C collation. Without -E the server is SQL_ASCII and clients get bytes for text.
  initdb -D "$PGDATA" -U postgres -E UTF8 --locale=C --auth-local=trust --auth-host=reject >/dev/null
  cat > "$PGDATA/pg_hba.conf" <<'EOF'
local all postgres trust
hostnossl all all all reject
hostssl chat chat_app all scram-sha-256
hostssl chat chat_replicator all scram-sha-256
hostssl chat cutover_control all scram-sha-256
host all all all reject
EOF
fi
exec postgres -D "$PGDATA" -c listen_addresses='*' -c ssl=on -c ssl_cert_file=/tls/tls.crt -c ssl_key_file=/tls/tls.key \
  -c wal_level=logical -c password_encryption=scram-sha-256 -c wal_sender_timeout=30s -c wal_receiver_timeout=30s \
  -c max_replication_slots=10 -c max_wal_senders=10 -c log_connections=on -c log_disconnections=on
"""
    pod = {"securityContext": {**POD_SECURITY, "runAsUser": 26, "runAsGroup": 26, "fsGroup": 26},
           "automountServiceAccountToken": False,
           "containers": [{"name": "postgres", "image": image, "imagePullPolicy": "IfNotPresent",
                           "command": ["sh", "-c", start], "env": [{"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                                                                    {"name": "PATH", "value": "/usr/lib/postgresql/18/bin:/usr/bin:/bin"}],
                           "ports": [{"containerPort": 5432}],
                           "readinessProbe": {"exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]}, "periodSeconds": 3},
                           "securityContext": {**RESTRICTED, "readOnlyRootFilesystem": False},
                           "resources": {"requests": {"cpu": "200m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
                           "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"},
                                            {"name": "run", "mountPath": "/var/run/postgresql"},
                                            {"name": "tls", "mountPath": "/tls", "readOnly": True}]}],
           "volumes": [{"name": "data", "emptyDir": {}}, {"name": "run", "emptyDir": {}},
                       {"name": "tls", "secret": {"secretName": "source-db-server", "defaultMode": 0o440}}]}
    deploy = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "source-db", "namespace": NS},
              "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": "source-db"}},
                       "template": {"metadata": {"labels": {"app": "source-db"}}, "spec": pod}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "source-db-rw", "namespace": NS},
           "spec": {"selector": {"app": "source-db"}, "ports": [{"name": "postgres", "port": 5432, "targetPort": 5432}]}}
    return [deploy, svc]


def moto():
    pod = {"enableServiceLinks": False, "securityContext": {**POD_SECURITY, "runAsUser": 10001, "runAsGroup": 10001},
           "automountServiceAccountToken": False,
           "containers": [{"name": "moto", "image": MOTO_IMAGE, "env": [
               {"name": "HOME", "value": "/tmp"}],
                           "ports": [{"containerPort": 5000}], "readinessProbe": {"tcpSocket": {"port": 5000}, "periodSeconds": 2},
                           "securityContext": RESTRICTED, "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
           "volumes": [{"name": "tmp", "emptyDir": {}}]}
    return [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "moto", "namespace": AWS_NS},
             "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "moto"}},
                      "template": {"metadata": {"labels": {"app": "moto"}}, "spec": pod}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "moto", "namespace": AWS_NS},
             "spec": {"selector": {"app": "moto"}, "ports": [{"port": 5000, "targetPort": 5000}]}}]


def objects(repo):
    here = str(repo / "infra/envs/target-workloads")
    sys.path.insert(0, here)  # objects.py imports its sibling render.py
    try:
        spec = importlib.util.spec_from_file_location("target_objects", f"{here}/objects.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(here)
    # The destination's own SeaweedFS pod (image, flags, security context); only storage and names differ.
    target = next(o for o in module.objects("unused") if o["kind"] == "Deployment")
    pod = {k: v for k, v in target["spec"]["template"]["spec"].items() if k != "serviceAccountName"}
    pod["volumes"] = [{"name": "data", "emptyDir": {}}, {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
                      {"name": "auth", "secret": {"secretName": "source-objects-auth", "defaultMode": 0o440,
                                                  "items": [{"key": "s3.json", "path": "s3.json"}]}}]
    labels = {"app": "source-objects"}
    return [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "source-objects", "namespace": AWS_NS},
             "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": pod}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "source-objects", "namespace": AWS_NS},
             "spec": {"selector": labels, "ports": [{"name": "s3", "port": 8333, "targetPort": 8333}]}}]


def app(render):
    env = {"APP_ENV": "aws", "DB_HOST": f"source-pgbouncer.{NS}.svc.cluster.local", "DB_PORT": "5432",
           "DB_NAME": "chat", "DB_SECRET_ID": SECRET_ID, "S3_BUCKET": BUCKET,
           "S3_ENDPOINT_URL": f"http://source-objects.{AWS_NS}.svc:8333", "S3_FORCE_PATH_STYLE": "true",
           "AWS_ENDPOINT_URL_SECRETS_MANAGER": f"http://moto.{AWS_NS}.svc:5000",
           "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION,
           "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/etc/cnpg-ca/ca.crt", "WRITE_BLOCKED": "false"}
    deploy = render.deployment("demo-chat", APP_IMAGE, {
        "name": "app", "env": [{"name": k, "value": v} for k, v in env.items()] + [
            {"name": "ADMIN_TOKEN", "valueFrom": {"secretKeyRef": {"name": "demo-chat-control", "key": "admin-token"}}},
            # The app identity of the bucket; the same keys reach the Secrets Manager stand-in, which accepts any.
            {"name": "AWS_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": {"name": "source-app-objects", "key": "access_key"}}},
            {"name": "AWS_SECRET_ACCESS_KEY", "valueFrom": {"secretKeyRef": {"name": "source-app-objects", "key": "secret_key"}}}],
        "ports": [{"containerPort": 8080}],
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}},
        "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "initialDelaySeconds": 10},
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
        "volumeMounts": [{"name": "ca", "mountPath": "/etc/cnpg-ca", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}]},
        [{"name": "ca", "secret": {"secretName": "source-db-ca", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
         {"name": "tmp", "emptyDir": {}}])
    deploy["spec"]["template"]["spec"]["securityContext"]["runAsUser"] = 10001
    return [deploy, render.service("demo-chat", 8080)]


class Source:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = Path(cfg["repo_root"])
        self.kube = Kube(cfg["kubeconfig"], cfg["cluster_uid"])
        self.vault = PlatformVault(cfg["vault_dir"])
        self.render = _module(self.repo, "source_workloads_render")

    def psql(self, sql, db="chat"):
        out = self.kube.exec(NS, "deploy/source-db", ["psql", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At",
                                                      "-h", "/var/run/postgresql", "-U", "postgres", "-d", db, "-f", "-"],
                             container="postgres", input=sql, timeout_s=120)
        return out.stdout.strip()

    def moto(self, script, stdin=""):
        """Run boto3 inside the moto Pod (it ships boto3): the stand-in AWS API is reachable only in-cluster.
        Values go on stdin, never into the script text (argv)."""
        code = ("import boto3,json,sys\nkw=dict(endpoint_url='http://127.0.0.1:5000',region_name='" + REGION + "',"
                "aws_access_key_id='local',aws_secret_access_key='local')\n" + script)
        return self.kube.exec(AWS_NS, "deploy/moto", ["python", "-c", code], container="moto", input=stdin, timeout_s=60).stdout

    def up(self):
        self.kube.verify_identity()
        creds = self.vault.put_once("source-credentials", {
            "chat_app": _random(32), "chat_replicator": _random(32), "admin_token": _random(40)})
        tls = self.vault.put_once("source-ca", _tls())
        LocalVmHost(self.cfg["host"]["generator"], self.cfg["host"]["state"]).import_image(
            self.cfg["images"]["app"], APP_NAMES)
        self.kube.apply([namespace(NS), namespace(AWS_NS)])
        self.kube.apply(moto())
        self.kube.apply([secret("source-db-ca", NS, {"ca.crt": tls["ca_crt"]}),
                         secret("source-db-server", NS, {"tls.crt": tls["tls_crt"], "tls.key": tls["tls_key"],
                                                         "ca.crt": tls["ca_crt"]}, "kubernetes.io/tls"),
                         secret("source-pgbouncer-auth", NS, {"userlist.txt": f'"chat_app" "{creds["chat_app"]}"\n'}),
                         secret("demo-chat-control", NS, {"admin-token": creds["admin_token"]})])
        ids = self.vault.put_once("source-objects-identities", {
            name: {"access_key": "SRC" + _random(17).upper(), "secret_key": _random(40)} for name in ("bootstrap", "client")})
        env = {"PATH": "/usr/bin:/bin"}
        for name, ident in ids.items():
            env[f"BRAVO_OBJECT_{name.upper()}_ACCESS_KEY"] = ident["access_key"]
            env[f"BRAVO_OBJECT_{name.upper()}_SECRET_KEY"] = ident["secret_key"]
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "s3.json"
            subprocess.run(["python3", str(self.repo / "infra/envs/target-workloads/object_exposure.py"), "render",
                            "--state", "closed", "--bucket", BUCKET, "--output", str(out)], env=env, check=True,
                           capture_output=True)
            s3 = out.read_text()
        self.kube.apply([secret("source-objects-auth", AWS_NS, {"s3.json": s3}),
                         secret("source-app-objects", NS, {"access_key": ids["client"]["access_key"],
                                                           "secret_key": ids["client"]["secret_key"]})])
        self.kube.apply(objects(self.repo))
        self.kube.apply(database(self.render.POSTGRES_IMAGE))
        self.kube.rollout(NS, "source-db", timeout_s=300)
        fresh = self.psql("SELECT count(*) FROM pg_database WHERE datname = 'chat';", db="postgres") == "0"
        if fresh:
            self.psql("CREATE ROLE chat_owner NOLOGIN; CREATE DATABASE chat OWNER chat_owner;", db="postgres")
        self.psql("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='chat_app') THEN CREATE ROLE chat_app LOGIN; END IF;"
                  " IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='chat_replicator') THEN CREATE ROLE chat_replicator LOGIN REPLICATION; END IF; END $$;\n"
                  f"ALTER ROLE chat_app PASSWORD '{creds['chat_app']}';\nALTER ROLE chat_replicator PASSWORD '{creds['chat_replicator']}';\n",
                  db="postgres")
        if self.psql("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename='messages';") == "0":
            self.psql((self.repo / "infra/envs/source-workloads/schema.sql").read_text())
        self.kube.apply(self.render.pooler())
        self.kube.rollout(AWS_NS, "moto")
        self.kube.rollout(AWS_NS, "source-objects", timeout_s=300)
        boot = ids["bootstrap"]
        self.moto(f"""from botocore.config import Config
a,k=sys.stdin.read().split()
s3=boto3.client('s3',endpoint_url='http://source-objects.{AWS_NS}.svc:8333',region_name='{REGION}',
  aws_access_key_id=a,aws_secret_access_key=k,config=Config(s3={{'addressing_style':'path'}}))
if '{BUCKET}' not in [b['Name'] for b in s3.list_buckets().get('Buckets',[])]: s3.create_bucket(Bucket='{BUCKET}')
""", stdin=f"{boot['access_key']} {boot['secret_key']}")
        self.moto(f"""sm=boto3.client('secretsmanager',**kw)
value=json.dumps({{'username':'chat_app','password':sys.stdin.read()}})
try: sm.create_secret(Name='{SECRET_ID}',SecretString=value)
except sm.exceptions.ResourceExistsException: pass
""", stdin=creds["chat_app"])
        self.kube.rollout(NS, "source-pgbouncer")
        self.kube.apply(app(self.render))
        if fresh:
            # A new server: connections opened against the old one (pooler cache, app pool) are stale.
            for name in ("source-pgbouncer", "demo-chat"):
                self.kube.run("rollout", "restart", "-n", NS, f"deployment/{name}")
                self.kube.rollout(NS, name, timeout_s=300)
        self.kube.rollout(NS, "demo-chat", timeout_s=300)
        return self.status()

    def status(self):
        return {"publication": self.psql("SELECT pubname FROM pg_publication;"),
                "messages": int(self.psql("SELECT count(*) FROM messages;")),
                "attachments": int(self.psql("SELECT count(*) FROM attachments;")),
                "wal_level": self.psql("SHOW wal_level;")}

    def secret_value(self):
        """The source DB credential as the source holds it: SecretString with its VersionId and AWSCURRENT."""
        out = self.moto(f"""sm=boto3.client('secretsmanager',**kw)
v=sm.get_secret_value(SecretId='{SECRET_ID}',VersionStage='AWSCURRENT')
print(json.dumps({{'version_id':v['VersionId'],'stages':v['VersionStages'],'secret_string':v['SecretString']}}))""")
        return json.loads(out.strip().splitlines()[-1])

    def seed(self, n, with_attachment=True):
        """Writes through the source app, the way users write (idempotency keys, one attachment)."""
        script = ("import json,sys,urllib.request,uuid\nn=int(sys.argv[1]);ok=0\n"
                  "for i in range(n):\n"
                  "  b=json.dumps({'idempotency_key':str(uuid.uuid4()),'author':'e2e-seed','body':f'source message {i}'}).encode()\n"
                  "  r=urllib.request.Request('http://127.0.0.1:8080/api/messages',data=b,method='POST',headers={'Content-Type':'application/json'})\n"
                  "  ok+=urllib.request.urlopen(r,timeout=10).status in (200,201)\n"
                  "print(ok)\n")
        pod = self.kube.ready_pods(NS, "app=demo-chat")[0]["metadata"]["name"]
        written = self.kube.exec(NS, pod, ["/app/.venv/bin/python", "-c", script, str(n)], container="app",
                                 timeout_s=120).stdout.strip()
        if with_attachment:
            boundary = uuid.uuid4().hex
            upload = ("import urllib.request\nb='BOUNDARY'\nbody=(f'--{b}\\r\\nContent-Disposition: form-data; name=\"file\"; filename=\"e2e.txt\"\\r\\n"
                      "Content-Type: text/plain\\r\\n\\r\\nbravo e2e attachment\\r\\n--{b}--\\r\\n').encode()\n"
                      "r=urllib.request.Request('http://127.0.0.1:8080/api/attachments/ATTACHMENT',data=body,method='PUT',"
                      "headers={'Content-Type':f'multipart/form-data; boundary={b}'})\n"
                      "print(urllib.request.urlopen(r,timeout=10).status)\n").replace("BOUNDARY", boundary).replace("ATTACHMENT", str(uuid.uuid4()))
            self.kube.exec(NS, pod, ["/app/.venv/bin/python", "-c", upload], container="app", timeout_s=60)
        return {"written": int(written or 0), **self.status()}


def main():
    command, cfg = sys.argv[1], json.loads(Path(sys.argv[2]).read_text())
    source = Source(cfg)
    if command == "up":
        print(json.dumps(source.up()))
    elif command == "seed":
        print(json.dumps(source.seed(int(sys.argv[3]))))
    elif command == "status":
        print(json.dumps(source.status()))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    t = time.monotonic()
    main()
    print(f"done in {time.monotonic() - t:.0f}s", file=sys.stderr)
