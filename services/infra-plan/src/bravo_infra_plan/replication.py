"""T8 forward replication as the migration stack sees it on the destination: what to look at, the
fixed read-only queries and the verdicts. Nothing here writes; the one write (the Subscription) is the
stack's `render_apply`.

The names are T8's. The external cluster is declared by its owner, the destination database render
(infra/envs/target-workloads/render.py, synced by T45's first build); this module only checks it.
True: observed done. False: not yet. A failed read or a target that is unsafe to copy into raises: HOLD.
"""

NAMESPACE, CLUSTER, DATABASE = "target-dev", "target-db", "chat"
EXTERNAL = "t8-source"
# T44's egress Service on the destination (infra/iac/environments/target-dev.json). The source
# certificate carries this name, so verify-full needs no observed address.
EXTERNAL_HOST = "source-db-for-replication.target-dev.svc"
PASSWORD = {"name": "t8-source-replication", "key": "password"}
SOURCE_CA = {"name": "t8-source-ca", "key": "ca.crt"}
SUBSCRIPTION = "t8_source_to_target"  # the PostgreSQL name; the CNPG object is t8-source-to-target
TABLES = ("attachments", "messages")  # the source publication source_chat

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


def credentials(cluster):
    """The Secrets the external cluster names exist with their keys (values are never read), and CNPG has
    loaded them. A Secret made after the Cluster reaches the instance only with the label cnpg.io/reload:
    without it the CA file is never written and the subscription cannot connect (CNPG 1.30.1, 2026-11-07)."""
    if not all(ref["key"] in (cluster.secret_keys(NAMESPACE, ref["name"]) or []) for ref in (PASSWORD, SOURCE_CA)):
        return False
    status = (cluster.get_object(_ref("Cluster", CLUSTER)) or {}).get("status", {})
    loaded = status.get("secretsResourceVersion", {}).get("externalClusterSecretVersion", {})
    return all(loaded.get(ref["name"]) for ref in (PASSWORD, SOURCE_CA))


def external_cluster(cluster):
    """target-db declares t8-source: verify-full on the kube DNS name, with the credentials above."""
    spec = (cluster.get_object(_ref("Cluster", CLUSTER)) or {}).get("spec", {})
    found = [x for x in spec.get("externalClusters", []) if x.get("name") == EXTERNAL]
    if len(found) != 1:
        return False
    params = found[0].get("connectionParameters", {})
    return (params.get("sslmode") == "verify-full" and params.get("host") == EXTERNAL_HOST
            and "hostaddr" not in params
            and found[0].get("password") == PASSWORD and found[0].get("sslRootCert") == SOURCE_CA)


def target_ready(cluster):
    """Safe to create the subscription with copy_data: it already exists, or every published table exists
    on the target and is empty. Rows already there would duplicate or retry on duplicate keys forever."""
    rows = _query(cluster, PRESENT)
    if rows is None:  # not seeing the primary is a hold, not a verdict
        raise RuntimeError("primary_unavailable")
    subscriptions, present = (int(x) for x in rows[0])
    if subscriptions == 1:
        return True
    if present != len(TABLES):
        raise RuntimeError("replication_target_schema_missing")
    filled = _query(cluster, FILLED)
    if filled is None:
        raise RuntimeError("primary_unavailable")
    if filled[0][0] != "f":
        raise RuntimeError("replication_target_not_empty")
    return True


def initial_copy(cluster):
    """The subscription is applied and every table finished its initial copy (pg_subscription_rel all 'r').
    No single primary right now (a switchover) is not done yet."""
    subscription = cluster.get_object(_ref("Subscription", SUBSCRIPTION.replace("_", "-")))
    if (subscription or {}).get("status", {}).get("applied") is not True:
        return False
    rows = _query(cluster, COPIED)
    if rows is None:
        return False
    total, pending = (int(x) for x in rows[0])
    return total > 0 and pending == 0
