"""T8 forward replication as the migration stack sees it on the destination: what to look at, the
fixed read-only queries and the verdicts. Nothing here writes; the one write (the Subscription) is the
stack's `render_apply`.

The names are T8's. The external cluster is declared by its owner, the destination database render
(infra/envs/target-workloads/render.py, synced by T45's first build); this module only checks it.
True: observed done. False: not yet. A failed read or a target that is unsafe to copy into: HOLD with a reason.
"""

from .executor import HOLD, StepOutcome

NAMESPACE, CLUSTER, DATABASE = "target-dev", "target-db", "chat"
# Exactly what render.py SOURCE_REPLICATION declares (a test pins the two equal). The host is T44's egress
# Service (infra/iac/environments/target-dev.json) resolved by kube DNS; the source certificate carries it.
DECLARATION = {
    "name": "t8-source",
    "connectionParameters": {
        "host": "source-db-for-replication.target-dev.svc", "port": "5432", "dbname": "chat",
        "user": "chat_replicator", "sslmode": "verify-full", "connect_timeout": "10",
        "application_name": "bravo_t8_target_subscriber",
    },
    "password": {"name": "t8-source-replication", "key": "password"},
    "sslRootCert": {"name": "t8-source-ca", "key": "ca.crt"},
}
SECRETS = (DECLARATION["password"], DECLARATION["sslRootCert"])
SUBSCRIPTION = "t8_source_to_target"  # the PostgreSQL name; the CNPG object is t8-source-to-target
TABLES = ("attachments", "messages")  # the source publication source_chat
# CNPG's own report on a subscription it cannot create. These do not heal by waiting.
STUCK = {"does not match host name": "source_tls_name_mismatch",  # source certificate lacks the DNS name
         "root certificate file": "source_ca_not_loaded"}  # the CA Secret was never loaded

# Fixed text: no caller input reaches SQL.
COPIED = ("SELECT count(*), count(*) FILTER (WHERE r.srsubstate <> 'r') FROM pg_subscription s "
          f"JOIN pg_subscription_rel r ON r.srsubid = s.oid WHERE s.subname = '{SUBSCRIPTION}'")
PRESENT = (f"SELECT (SELECT count(*) FROM pg_subscription WHERE subname = '{SUBSCRIPTION}'), "
           "(SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN ("
           + ", ".join(f"'{t}'" for t in TABLES) + "))")
FILLED = "SELECT " + " OR ".join(f"EXISTS (SELECT 1 FROM public.{t})" for t in TABLES)


def _ref(kind, name):
    return {"apiVersion": "postgresql.cnpg.io/v1", "kind": kind, "metadata": {"namespace": NAMESPACE, "name": name}}


def _query(cluster, sql):
    return cluster.primary_query(NAMESPACE, CLUSTER, DATABASE, sql)


def _target_db(cluster):
    return cluster.get_object(_ref("Cluster", CLUSTER)) or {}


def external_cluster(cluster):
    """target-db declares t8-source exactly as the render does: kube DNS, verify-full, no hostaddr."""
    found = [x for x in _target_db(cluster).get("spec", {}).get("externalClusters", [])
             if x.get("name") == DECLARATION["name"]]
    return len(found) == 1 and all(found[0].get(key) == value for key, value in DECLARATION.items())


def credentials(cluster):
    """The Secrets the external cluster names exist with their keys (values are never read), and CNPG has
    loaded them. A Secret made after the Cluster reaches the instance only with the label cnpg.io/reload:
    without it the CA file is never written and the subscription cannot connect (CNPG 1.30.1, 2026-11-07)."""
    if not all(ref["key"] in (cluster.secret_keys(NAMESPACE, ref["name"]) or []) for ref in SECRETS):
        return False
    loaded = (_target_db(cluster).get("status", {}).get("secretsResourceVersion", {})
              .get("externalClusterSecretVersion", {}))
    return all(loaded.get(ref["name"]) for ref in SECRETS)


def target_ready(cluster):
    """Safe to create the subscription with copy_data: it already exists, or every published table exists
    on the target and is empty. Rows already there would duplicate or retry on duplicate keys forever."""
    rows = _query(cluster, PRESENT)
    if rows is None:  # not seeing the primary is a hold, not a verdict
        raise StepOutcome(HOLD, "primary_unavailable")
    subscriptions, present = (int(x) for x in rows[0])
    if subscriptions == 1:
        return True
    if present != len(TABLES):
        raise StepOutcome(HOLD, "replication_target_schema_missing")
    filled = _query(cluster, FILLED)
    if filled is None:
        raise StepOutcome(HOLD, "primary_unavailable")
    if filled[0][0] != "f":
        raise StepOutcome(HOLD, "replication_target_not_empty")
    return True


def initial_copy(cluster):
    """The subscription is applied and every table finished its initial copy (pg_subscription_rel all 'r').
    No single primary right now (a switchover) is not done yet."""
    status = (cluster.get_object(_ref("Subscription", SUBSCRIPTION.replace("_", "-"))) or {}).get("status", {})
    if status.get("applied") is not True:
        reason = next((r for text, r in STUCK.items() if text in (status.get("message") or "")), None)
        if reason:
            raise StepOutcome(HOLD, reason)
        return False
    rows = _query(cluster, COPIED)
    if rows is None:
        return False
    total, pending = (int(x) for x in rows[0])
    return total > 0 and pending == 0
