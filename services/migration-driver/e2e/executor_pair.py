"""A further Cutover Executor on the platform for one more DB pair (ADR-0011: one Executor per pair).

`state` gives the instance its own state database on the platform DB, the way the first Executor's was made
(2026-11-08 readback: CNPG managed roles with basic-auth Secrets, hba lines per role; the platform-data renderer
does not hold them). Its advisory lock and schema are per database, so the two instances never contend.

    roles      cutover_executor_<pair>_migration (owns the database, runs DDL once), ..._runtime (DML only)
    database   executor_<pair>
    Secrets    platform-state/<role>-db (CNPG), bravo-platform/cutover-executor-<pair>-{migration,state,state-ca}

Passwords are made in memory and go only into Secrets and psql stdin; nothing secret is printed or written
locally. The first Executor's Secrets and roles are read for their shape only and never changed.

usage: python executor_pair.py state <executor.json>
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


def main():
    command, cfg = sys.argv[1], json.loads(Path(sys.argv[2]).read_text())
    instance = Instance(cfg)
    if command == "state":
        print(json.dumps(instance.state()))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
