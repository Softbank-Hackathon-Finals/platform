"""Bounded operator patches for existing CNPG/pooler resources; no credentials.

Render/review before applying. A JSON Patch resourceVersion test rejects stale
observations. This does not execute SQL, enable an app role, or claim readiness.

Object names come from the environment's Site (db_cluster, db_namespace,
pgbouncer_deployment). Unset fields keep the original deployment rule
{side}-db, {side}-dev and {side}-pgbouncer, so existing callers are unchanged.
"""

from dataclasses import dataclass

from bravo_cutover.sites import Site

OWNER_KEY = "bravo.dev/app-login-owner"
OWNER = "cutover-executor"
CONTROL_SECRET = "cutover-db-control"
TRUST_SECRET = "cutover-backend-ca"
SIDES = ("source", "target")


@dataclass(frozen=True)
class ObjectNames:
    cluster: str
    namespace: str
    pooler: str


def object_names(side: str, site: Site | None = None) -> ObjectNames:
    """The CNPG Cluster, its namespace and the pooler Deployment this side expects.
    The environment kind is not consulted: names are configuration, not a kind rule."""
    if side not in SIDES:
        raise ValueError("unknown cutover side")
    cluster, namespace, pooler = f"{side}-db", f"{side}-dev", f"{side}-pgbouncer"
    if site is not None:
        cluster = site.db_cluster or cluster
        namespace = site.db_namespace or namespace
        pooler = site.pgbouncer_deployment or pooler
    return ObjectNames(cluster, namespace, pooler)


def cluster_patch(cluster: dict, *, side: str, site: Site | None = None) -> list[dict]:
    names = object_names(side, site)
    meta, spec = cluster["metadata"], cluster["spec"]
    if (cluster.get("kind") != "Cluster" or meta.get("name") != names.cluster
            or meta.get("namespace") != names.namespace):
        raise ValueError("unexpected database resource")
    roles = spec["managed"]["roles"]
    annotations = meta.get("annotations", {})
    if annotations.get(OWNER_KEY) not in (None, OWNER):
        raise ValueError("another owner controls app LOGIN")
    app_roles = [r for r in roles if r["name"] == "chat_app"]
    if len(app_roles) > 1 or (not app_roles and annotations.get(OWNER_KEY) != OWNER):
        raise ValueError("app role ownership is not established")
    control = {"name": "cutover_control", "ensure": "present", "login": True,
               "superuser": False, "createdb": False, "createrole": False,
               "replication": False, "bypassrls": False, "inRoles": ["pg_read_all_stats"],
               "passwordSecret": {"name": CONTROL_SECRET}}
    existing = [r for r in roles if r["name"] == "cutover_control"]
    if existing and (len(existing) != 1 or any(existing[0].get(k) != v for k, v in control.items())):
        raise ValueError("existing control role contract differs")
    desired = [r for r in roles if r["name"] not in {"chat_app", "cutover_control"}] + (existing or [control])
    hba = spec["postgresql"]["pg_hba"][:]
    rule = "hostssl chat cutover_control all scram-sha-256"
    if "host all all all reject" not in hba:
        raise ValueError("expected final deny rule is missing")
    if rule not in hba:
        hba.insert(hba.index("host all all all reject"), rule)
    patches = [{"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
    if annotations.get(OWNER_KEY) != OWNER:
        if "annotations" not in meta:
            patches.append({"op": "add", "path": "/metadata/annotations", "value": {OWNER_KEY: OWNER}})
        else:
            patches.append({"op": "add", "path": "/metadata/annotations/bravo.dev~1app-login-owner", "value": OWNER})
    if desired != roles:
        patches.append({"op": "replace", "path": "/spec/managed/roles", "value": desired})
    if hba != spec["postgresql"]["pg_hba"]:
        patches.append({"op": "replace", "path": "/spec/postgresql/pg_hba", "value": hba})
    return patches


def pooler_trust_patch(deployment: dict, *, side: str, site: Site | None = None) -> list[dict]:
    names = object_names(side, site)
    meta = deployment["metadata"]
    if (deployment.get("kind") != "Deployment" or meta.get("name") != names.pooler
            or meta.get("namespace") != names.namespace or deployment["spec"]["replicas"] != 1):
        raise ValueError("unexpected singleton pooler")
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    positions = [i for i, volume in enumerate(volumes) if volume["name"] == "ca"]
    if len(positions) != 1 or "secret" not in volumes[positions[0]]:
        raise ValueError("expected backend CA Secret mount is missing")
    position = positions[0]
    patches = [{"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
    if volumes[position]["secret"]["secretName"] != TRUST_SECRET:
        patches.append({"op": "replace", "path": f"/spec/template/spec/volumes/{position}/secret/secretName",
                        "value": TRUST_SECRET})
    return patches
