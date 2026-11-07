"""Make a source/destination DB pair cutover-ready for its own Executor (T12 platform integration contract).

The shape is the live onprem pair's catalog (2026-11-08 readback of source-dev): the `cutover` schema and its
barrier/probe tables in both publications, the four fixed control functions (`provision.control_sql`), a
`cutover_control` login with pg_read_all_stats only, the forward subscription enabled and the reverse one
created disabled (copy_data false, origin none) with its slot already on the destination.

Order matters: control_sql refuses a database without its subscription, and a subscriber sees the cutover
tables only after its publisher adds them and it refreshes (copy_data false: no business table is copied again).
The forward subscription is the T45 migration stack's (CNPG Subscription t8-source-to-target); this script
never creates or changes it beyond that refresh. Credentials are read in the cluster or from the platform
vault and go to psql on stdin; nothing secret is printed.

usage: python cutover_pair.py ready <pair.json>    idempotent; prints the readback
       python cutover_pair.py status <pair.json>
"""

import json
import secrets
import string
import sys
from pathlib import Path

from bravo_migration.kube import Kube
from bravo_migration.vault import PlatformVault

FORWARD, REVERSE = "t8_source_to_target", "t8_target_to_source"
CUTOVER_TABLES = "cutover.barrier, cutover.probe"


def _random(n):
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def _literal(value):
    if "'" in value or "\\" in value:
        raise ValueError("unexpected character in a generated credential")
    return f"'{value}'"


def _provision(repo):
    sys.path.insert(0, str(repo / "services/cutover-executor/src"))
    from bravo_cutover import provision  # noqa: PLC0415 - the reviewed SQL, not a copy
    return provision


class Side:
    def __init__(self, kube, namespace, target, container):
        self.kube, self.namespace, self.target, self.container = kube, namespace, target, container

    def psql(self, sql, db="chat"):
        argv = ["psql", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At", "-U", "postgres", "-d", db, "-f", "-"]
        if self.target.startswith("deploy/"):  # the source stand-in listens on a socket directory
            argv[6:6] = ["-h", "/var/run/postgresql"]
        return self.kube.exec(self.namespace, self.target, argv, container=self.container, input=sql,
                              timeout_s=180).stdout.strip()

    def secret(self, name, key):
        value = self.kube.secret_value(self.namespace, name, key)
        if not value:
            raise SystemExit(f"{self.namespace}/{name} has no {key}: the build (T45) or source fixture comes first")
        return value


class Pair:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = Path(cfg["repo_root"])
        s, t = cfg["source"], cfg["target"]
        self.source = Side(Kube(s["kubeconfig"], s["cluster_uid"]), s["namespace"], "deploy/source-db", "postgres")
        self.target = Side(Kube(t["kubeconfig"], t["cluster_uid"]), t["namespace"], "target-db-1", "postgres")
        self.vault = PlatformVault(cfg["vault_dir"])

    def ready(self):
        for side in (self.source, self.target):
            side.kube.verify_identity()
        provision = _provision(self.repo)
        ca = self.target.secret("target-db-ca", "ca.crt")
        self.source.kube.apply([{"apiVersion": "v1", "kind": "Secret", "metadata": {
            "name": "target-db-ca", "namespace": self.source.namespace}, "stringData": {"ca.crt": ca}}])
        control = self.vault.put_once("source-cutover-control", {"password": _random(32)})
        # Source: the Executor's control login (the CNPG source declares it as a managed role; the stand-in has none).
        self.source.psql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='cutover_control') THEN "
                         "CREATE ROLE cutover_control LOGIN; END IF; END $$;\n"
                         f"ALTER ROLE cutover_control LOGIN PASSWORD {_literal(control['password'])};\n", db="postgres")
        # Destination: chat_app takes the source's password (the app credential moves with the app, T9), because
        # one pooler userlist entry logs in to the source before cutover and to the destination after it.
        # It stays NOLOGIN until the Executor opens it.
        app = (self.vault.get("source-credentials") or {}).get("chat_app") or sys.exit("run source_fixture up first")
        self.target.psql(f"ALTER ROLE chat_app NOLOGIN PASSWORD {_literal(app)};\n", db="postgres")
        self._poolers(app, control["password"])
        if self.target.psql(f"SELECT count(*) FROM pg_subscription WHERE subname='{FORWARD}';") != "1":
            raise SystemExit("the forward subscription is missing: run the T45 migration stack first")
        # Destination control surface, then its publication; the source's reverse subscription reads it.
        self.target.psql(provision.control_sql(FORWARD))
        self._publish(self.target, "target_chat")
        # The reverse slot on the destination first, so the source's subscription never needs to create it.
        self.target.psql(f"SELECT pg_create_logical_replication_slot('{REVERSE}', 'pgoutput') WHERE NOT EXISTS "
                         f"(SELECT FROM pg_replication_slots WHERE slot_name='{REVERSE}');")
        target_repl = self.target.secret("target-replication", "password")
        # TLS checks the destination server's own name; the address is the source cluster's egress Service to it
        # (iac tailnet-egress target-db-for-replication), the same host/hostaddr split as the Executor's DSNs.
        service = self.source.kube.get("service", "target-db-for-replication", self.source.namespace)
        if service is None:
            raise SystemExit("no egress Service to the destination DB yet: sync the source cluster's iac chart")
        egress = service["spec"]["clusterIP"]
        conninfo = (f"host=target-db-rw.target-dev.svc.cluster.local hostaddr={egress} port=5432 dbname=chat "
                    f"user=chat_replicator password={target_repl} sslmode=verify-full sslrootcert=/target-ca/ca.crt "
                    "connect_timeout=10")
        self.source.psql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_subscription WHERE subname='" + REVERSE + "') THEN "
                         f"EXECUTE format('CREATE SUBSCRIPTION {REVERSE} CONNECTION %L PUBLICATION target_chat "
                         "WITH (enabled = false, connect = false, create_slot = false, slot_name = %L, "
                         f"copy_data = false, origin = none)', {_literal(conninfo)}, '{REVERSE}'); END IF; END $$;\n")
        self.source.psql(provision.control_sql(REVERSE))
        self._publish(self.source, "source_chat")
        # The forward subscriber sees the source's cutover tables only after a refresh (no business table re-copy).
        self.target.psql(f"ALTER SUBSCRIPTION {FORWARD} REFRESH PUBLICATION WITH (copy_data = false);")
        return self.status()

    def _poolers(self, app, source_control):
        """Both poolers admit the Executor's admin login; the destination pooler starts on the source DB
        (T30 pooler.py, bound to the destination's egress Service UID) and trusts both CAs."""
        target_control = self.target.secret("cutover-db-control", "password")
        users = lambda control: f'"chat_app" "{app}"\n"cutover_control" "{control}"\n'  # noqa: E731
        self.source.kube.apply([{"apiVersion": "v1", "kind": "Secret", "metadata": {
            "name": "source-pgbouncer-auth", "namespace": self.source.namespace},
            "stringData": {"userlist.txt": users(source_control)}}])
        self.source.kube.run("rollout", "restart", "-n", self.source.namespace, "deployment/source-pgbouncer")
        self.source.kube.rollout(self.source.namespace, "source-pgbouncer")
        egress = self.target.kube.get("service", "source-db-for-pooler", self.target.namespace)
        if egress is None:
            raise SystemExit("no egress Service to the source DB yet: sync the destination's iac chart")
        source_ca = self.source.secret("source-db-ca", "ca.crt")
        target_ca = self.target.secret("target-db-ca", "ca.crt")
        self.target.kube.apply([
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "target-pgbouncer-auth",
                                                               "namespace": self.target.namespace},
             "stringData": {"userlist.txt": users(target_control)}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "cutover-backend-ca",
                                                               "namespace": self.target.namespace},
             "stringData": {"ca.crt": source_ca.strip() + "\n" + target_ca.strip() + "\n"}}])
        here = str(self.repo / "infra/envs/target-workloads")
        sys.path.insert(0, here)
        try:
            import pooler  # noqa: PLC0415 - T30's reviewed renderer
        finally:
            sys.path.remove(here)
        self.target.kube.apply(pooler.pooler(egress["spec"]["clusterIP"], egress["metadata"]["uid"]))
        self.target.kube.rollout(self.target.namespace, "target-pgbouncer")

    @staticmethod
    def _publish(side, publication):
        missing = side.psql(f"SELECT count(*) FROM (VALUES ('barrier'), ('probe')) t(n) WHERE NOT EXISTS ("
                            f"SELECT FROM pg_publication_tables WHERE pubname='{publication}' "
                            "AND schemaname='cutover' AND tablename=t.n);")
        if missing == "2":
            side.psql(f"ALTER PUBLICATION {publication} ADD TABLE {CUTOVER_TABLES};")
        elif missing != "0":
            raise SystemExit(f"{publication} has only one cutover table: fix it by hand, never half-apply")

    def status(self):
        q = ("SELECT json_build_object("
             "'publication', (SELECT json_agg(schemaname||'.'||tablename ORDER BY 1) FROM pg_publication_tables), "
             "'subscriptions', (SELECT json_agg(json_build_object('name', subname, 'enabled', subenabled, "
             "'origin', suborigin) ORDER BY subname) FROM pg_subscription), "
             "'functions', (SELECT json_agg(proname ORDER BY 1) FROM pg_proc p JOIN pg_namespace n "
             "ON n.oid=p.pronamespace WHERE nspname='cutover'), "
             "'chat_app_login', (SELECT rolcanlogin FROM pg_roles WHERE rolname='chat_app'), "
             "'cutover_control', (SELECT rolcanlogin FROM pg_roles WHERE rolname='cutover_control'));")
        return {"source": json.loads(self.source.psql(q)), "target": json.loads(self.target.psql(q))}


def main():
    command, cfg = sys.argv[1], json.loads(Path(sys.argv[2]).read_text())
    pair = Pair(cfg)
    if command == "ready":
        print(json.dumps(pair.ready()))
    elif command == "status":
        print(json.dumps(pair.status()))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
