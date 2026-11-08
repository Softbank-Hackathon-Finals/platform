"""A further Cutover Executor on the platform for one more DB pair (ADR-0011: one Executor per pair).

`state` gives the instance its own state database on the platform DB, the way the first Executor's was made
(2026-11-08 readback: CNPG managed roles with basic-auth Secrets, hba lines per role; the platform-data renderer
does not hold them). Its advisory lock and schema are per database, so the two instances never contend.

    roles      cutover_executor_<pair>_migration (owns the database, runs DDL once), ..._runtime (DML only)
    database   executor_<pair>
    Secrets    platform-state/<role>-db (CNPG), bravo-platform/cutover-executor-<pair>-{migration,state,state-ca}

Passwords are made in memory and go only into Secrets and psql stdin; nothing secret is printed or written
locally. The first Executor's Secrets and roles are read for their shape only and never changed.

`deploy` gives it its runtime: sites.json for the pair (DSNs keep each server's TLS name in host= and reach it
through this instance's own platform egress Service in hostaddr=, as the first instance's do), a token-less
kubeconfig to each cluster's API proxy through its own egress Service (tls-server-name keeps the proxy's name,
so a second route to the same proxy never resolves to the other instance's), its own CA and server certificate,
API and command tokens, and a namespaced Role on each cluster for its tailnet tag, as narrow as the first
instance's (pods get/list, exec only into the named pooler and app Pods, the pooler ConfigMap).

usage: python executor_pair.py state <executor.json>
       python executor_pair.py deploy <executor.json>
"""

import base64
import json
import re
import secrets
import string
import sys
import time
from pathlib import Path

from bravo_migration.kube import Kube
from bravo_migration.vault import PlatformVault

PLATFORM_NS, STATE_NS, DB_CLUSTER, DB_POD = "bravo-platform", "platform-state", "platform-db", "platform-db-1"
REJECT = "host all all all reject"


def _random(n):
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def _secret(name, namespace, data, kind="Opaque"):
    return {"apiVersion": "v1", "kind": "Secret", "type": kind, "metadata": {"name": name, "namespace": namespace},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}


def _dsn(template, *, user, dbname, password):
    """The first instance's DSN with only user, database and password replaced (host, TLS and timeouts kept)."""
    parts = dict(p.split("=", 1) for p in template.split())
    if set(parts) - {"host", "hostaddr", "port", "dbname", "user", "password", "sslmode", "sslrootcert",
                     "connect_timeout", "application_name"}:
        raise SystemExit("unexpected key in the first Executor's state DSN; compare by hand first")
    if parts.get("sslmode") != "verify-full":
        raise SystemExit("the first Executor's state DSN is not verify-full")
    parts.update(user=user, dbname=dbname, password=password)
    return " ".join(f"{k}={v}" for k, v in parts.items())


class Instance:
    def __init__(self, cfg):
        self.cfg, self.pair = cfg, cfg["pair"]
        if not re.fullmatch(r"[a-z][a-z0-9]{0,15}", self.pair):
            raise SystemExit("pair must be a short lowercase name")
        self.kube = Kube(cfg["kubeconfig"], cfg["cluster_uid"])
        self.name = f"cutover-executor-{self.pair}"
        self.db = f"executor_{self.pair}"
        self.roles = {kind: f"cutover_executor_{self.pair}_{kind}" for kind in ("migration", "runtime")}
        self.repo = Path(cfg["repo_root"])

    def psql(self, sql, db="postgres"):
        return self.kube.exec(STATE_NS, DB_POD, ["psql", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At", "-U", "postgres",
                                                 "-d", db, "-f", "-"], container="postgres", input=sql,
                              timeout_s=120).stdout.strip()

    def _cluster_roles(self):
        cluster = self.kube.get("cluster.postgresql.cnpg.io", DB_CLUSTER, STATE_NS)
        return cluster, {r["name"]: r for r in cluster["spec"]["managed"]["roles"]}

    def state(self):
        self.kube.verify_identity()
        cluster, roles = self._cluster_roles()
        first = roles.get("cutover_executor_runtime")
        if first is None:
            raise SystemExit("the first Executor's runtime role is missing; its shape is the template")
        passwords = {}
        for kind, role in self.roles.items():
            if role not in roles:
                passwords[kind] = _random(32)
                self.kube.apply([_secret(f"{role.replace('_', '-')}-db", STATE_NS,
                                         {"username": role, "password": passwords[kind]}, "kubernetes.io/basic-auth")])
        hba = cluster["spec"]["postgresql"]["pg_hba"]
        if hba[-1] != REJECT:
            raise SystemExit("platform-db pg_hba does not end with the reject line; refusing to edit it")
        lines = [f"hostssl {self.db} {role} all scram-sha-256" for role in self.roles.values()]
        patch = [{"op": "add", "path": "/spec/managed/roles/-",
                  "value": {**first, "name": role, "passwordSecret": {"name": f"{role.replace('_', '-')}-db"}}}
                 for kind, role in self.roles.items() if role not in roles]
        if any(line not in hba for line in lines):
            patch.append({"op": "test", "path": "/spec/postgresql/pg_hba", "value": hba})
            patch.append({"op": "replace", "path": "/spec/postgresql/pg_hba",
                          "value": hba[:-1] + [line for line in lines if line not in hba] + [REJECT]})
        if patch:
            self.kube.run("patch", "cluster.postgresql.cnpg.io", DB_CLUSTER, "-n", STATE_NS, "--type=json",
                          "-p", json.dumps(patch))
        for _ in range(60):
            have = self.psql("SELECT count(*) FROM pg_roles WHERE rolname IN ("
                             + ",".join(f"'{r}'" for r in self.roles.values()) + ") AND rolcanlogin;")
            if have == "2":
                break
            time.sleep(2)
        else:
            raise SystemExit("CNPG did not create the instance's roles in time")
        if self.psql(f"SELECT count(*) FROM pg_database WHERE datname='{self.db}';") == "0":
            self.psql(f"CREATE DATABASE {self.db} OWNER {self.roles['migration']};")
        self.psql(f"REVOKE ALL ON DATABASE {self.db} FROM PUBLIC;\n"
                  f"GRANT CONNECT ON DATABASE {self.db} TO {self.roles['runtime']};\n")
        template = self.kube.secret_value(PLATFORM_NS, "cutover-executor-state", "dsn")
        ca = self.kube.secret_value(PLATFORM_NS, "cutover-executor-state-ca", "ca.crt")
        if not template or not ca:
            raise SystemExit("the first Executor's state DSN or CA is missing")
        state = []
        for kind, secret_name in (("migration", f"{self.name}-migration"), ("runtime", f"{self.name}-state")):
            password = passwords.get(kind) or self.kube.secret_value(STATE_NS, f"{self.roles[kind].replace('_', '-')}-db",
                                                                     "password")
            state.append(_secret(secret_name, PLATFORM_NS, {"dsn": _dsn(template, user=self.roles[kind], dbname=self.db,
                                                                          password=password)}))
        state.append(_secret(f"{self.name}-state-ca", PLATFORM_NS, {"ca.crt": ca}))
        self.kube.apply(state)
        return self.readback()

    def readback(self):
        return {"database": self.psql(f"SELECT datname||':'||pg_get_userbyid(datdba) FROM pg_database "
                                      f"WHERE datname='{self.db}';"),
                "roles": self.psql("SELECT string_agg(rolname, ',' ORDER BY rolname) FROM pg_roles WHERE rolname IN ("
                                   + ",".join(f"'{r}'" for r in self.roles.values()) + ") AND rolcanlogin;"),
                "hba": [line for line in self._cluster_roles()[0]["spec"]["postgresql"]["pg_hba"] if self.db in line],
                "secrets": sorted(s["metadata"]["name"] for s in self.kube.get("secret", namespace=PLATFORM_NS)["items"]
                                  if s["metadata"]["name"].startswith(self.name))}


def _tls(name):
    """A CA for this instance only (its key is not kept) and the server certificate its clients verify."""
    import ipaddress
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(UTC)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(days=90)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .sign(ca_key, hashes.SHA256()))
    names = [name, f"{name}.{PLATFORM_NS}", f"{name}.{PLATFORM_NS}.svc", f"{name}.{PLATFORM_NS}.svc.cluster.local"]
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
            # 127.0.0.1: the operator's test harness reaches it through kubectl port-forward.
            .add_extension(x509.SubjectAlternativeName([*map(x509.DNSName, names),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    return {"ca.crt": ca.public_bytes(pem).decode(), "tls.crt": leaf.public_bytes(pem).decode(),
            "tls.key": key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}


def _role(namespace, tag, exec_pods, configmap, name):
    rules = [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
             {"apiGroups": [""], "resources": ["pods/exec"], "resourceNames": sorted(exec_pods), "verbs": ["get", "create"]},
             {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [configmap], "verbs": ["get", "patch"]}]
    meta = {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/part-of": "bravo-cutover"}}
    return [{"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta, "rules": rules},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta,
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
             "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": tag}]}]


def _resource_id(cluster_uid, namespace):
    import hashlib
    return "bravo-db-" + hashlib.sha256(f"{cluster_uid}/{namespace}/chat".encode()).hexdigest()[:32]


class Deploy(Instance):
    """The runtime of one further instance; see the module text."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.src = Kube(cfg["source"]["kubeconfig"], cfg["source"]["cluster_uid"])
        self.dst = Kube(cfg["target"]["kubeconfig"], cfg["target"]["cluster_uid"])
        self.vault = PlatformVault(cfg["vault_dir"])
        self.tag = f"tag:bravo-cutover-control-{self.pair}"

    def _egress(self, route):
        svc = self.kube.get("service", f"{route}-for-cutover", PLATFORM_NS)
        if svc is None:
            raise SystemExit(f"no platform egress Service {route}-for-cutover yet: sync the platform iac chart")
        return svc["spec"]["clusterIP"]

    def _dsn(self, side, kind, user, password):
        db = {"source": f"source-db-rw.{self.cfg['source']['namespace']}.svc.cluster.local",
              "target": "target-db-rw.target-dev.svc.cluster.local"}[side]
        pooler = {"source": f"source-pgbouncer.{self.cfg['source']['namespace']}.svc.cluster.local",
                  "target": "target-pgbouncer.target-dev.svc.cluster.local"}[side]
        host, route, port = ((db, f"{side}-{self.pair}-db", 5432) if kind == "db"
                             else (pooler, f"{side}-{self.pair}-pooler", 6432))
        dbname = "pgbouncer" if kind == "admin" else "chat"
        return (f"user={user} dbname={dbname} host={host} hostaddr={self._egress(route)} port={port} "
                f"password={password} sslmode=verify-full sslrootcert=/run/executor/{side}-ca.crt connect_timeout=5")

    def _site(self, side, env, namespace, uid, control, app):
        prefix = "source" if side == "source" else "target"
        return {"environment_id": env, "resource_id": _resource_id(uid, namespace), "connection_ref": f"cutover/{side}",
                "kube_context": side, "namespace": namespace, "app_selector": "app=demo-chat",
                "pgbouncer_selector": f"app={prefix}-pgbouncer", "pgbouncer_configmap": f"{prefix}-pgbouncer-config",
                "db_host": f"{prefix}-db-rw.{namespace}.svc.cluster.local",
                "subscription": "t8_target_to_source" if side == "source" else "t8_source_to_target",
                "subscription_owner": "postgres", "control_functions": True,
                "system_roles": ["cutover_control", "chat_replicator", "postgres"],
                "pgbouncer_admin_dsn": self._dsn(side, "admin", "cutover_control", control),
                "pgbouncer_app_dsn": self._dsn(side, "pooler", "chat_app", app),
                "db_control_dsn": self._dsn(side, "db", "cutover_control", control),
                "db_app_dsn": self._dsn(side, "db", "chat_app", app)}

    def deploy(self):
        for kube in (self.kube, self.src, self.dst):
            kube.verify_identity()
        s_ns = self.cfg["source"]["namespace"]
        app = (self.vault.get("source-credentials") or {}).get("chat_app")
        s_control = (self.vault.get("source-cutover-control") or {}).get("password")
        t_control = self.dst.secret_value("target-dev", "cutover-db-control", "password")
        if not (app and s_control and t_control):
            raise SystemExit("pair credentials missing: run source_fixture up and cutover_pair ready first")
        sites = {"origin": self.cfg["source"]["environment_id"], "sites": {
            self.cfg["source"]["environment_id"]: self._site("source", self.cfg["source"]["environment_id"], s_ns,
                                                             self.cfg["source"]["cluster_uid"], s_control, app),
            self.cfg["target"]["environment_id"]: self._site("target", self.cfg["target"]["environment_id"], "target-dev",
                                                             self.cfg["target"]["cluster_uid"], t_control, app)}}
        tailnet = self.cfg["tailnet"]
        kubeconfig = {"apiVersion": "v1", "kind": "Config", "users": [{"name": "tailnet", "user": {}}],
                      "clusters": [{"name": side, "cluster": {
                          "server": f"https://{side}-{self.pair}-api-for-cutover.{PLATFORM_NS}.svc:443",
                          "tls-server-name": f"{proxy}.{tailnet}"}}
                          for side, proxy in (("source", "bravo-source-api"),  # Executor auth proxy (#978), not the human one
                                              ("target", f"bravo-target-{self.pair}-operator"))],
                      "contexts": [{"name": side, "context": {"cluster": side, "user": "tailnet"}}
                                   for side in ("source", "target")]}
        tls = self.vault.put_once(f"{self.name}-tls", _tls(self.name))
        tokens = self.vault.put_once(f"{self.name}-tokens", {"api": _random(48), "command": _random(48)})
        runtime = {"sites.json": json.dumps(sites), "kubeconfig": json.dumps(kubeconfig), "api-token": tokens["api"],
                   "tls.crt": tls["tls.crt"], "tls.key": tls["tls.key"],
                   "source-ca.crt": self.src.secret_value(s_ns, "source-db-ca", "ca.crt"),
                   "target-ca.crt": self.dst.secret_value("target-dev", "target-db-ca", "ca.crt")}
        self.kube.apply([_secret(f"{self.name}-runtime", PLATFORM_NS, runtime),
                         _secret(f"{self.name}-command", PLATFORM_NS, {"token": tokens["command"]}),
                         _secret(f"{self.name}-ca", PLATFORM_NS, {"ca.crt": tls["ca.crt"]})])
        # Each cluster lets this instance's tailnet tag into the pair's namespace only, as narrowly as the first.
        pods = lambda kube, ns, selectors: [p["metadata"]["name"] for sel in selectors  # noqa: E731
                                            for p in kube.ready_pods(ns, sel)]
        self.src.apply(_role(s_ns, self.tag, pods(self.src, s_ns, ["app=source-pgbouncer", "app=demo-chat"]),
                             "source-pgbouncer-config", self.name))
        self.dst.apply(_role("target-dev", self.tag, pods(self.dst, "target-dev", ["app=target-pgbouncer", "app=demo-chat"]),
                             "target-pgbouncer-config", self.name))
        image = self.kube.get("deployment", "cutover-executor", PLATFORM_NS)["spec"]["template"]["spec"]["containers"][0]["image"]
        sys.path.insert(0, str(self.repo / "services/cutover-executor/src"))
        from bravo_cutover.deploy import manifests  # noqa: PLC0415
        self.kube.apply(manifests(image, pair=self.pair)["items"])
        self.kube.rollout(PLATFORM_NS, self.name, timeout_s=300)
        return {"deployment": self.name, "image_digest": image.split("@")[-1][:19],
                "routes": sorted(f"{side}-{self.pair}-{k}" for side in ("source", "target") for k in ("db", "pooler", "api"))}


def main():
    command, cfg = sys.argv[1], json.loads(Path(sys.argv[2]).read_text())
    if command == "state":
        print(json.dumps(Instance(cfg).state()))
    elif command == "deploy":
        print(json.dumps(Deploy(cfg).deploy()))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
