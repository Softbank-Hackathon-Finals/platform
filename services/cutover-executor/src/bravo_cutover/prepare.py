"""Bounded operator patches for existing CNPG/pooler resources; no credentials.

Render/review before applying. A JSON Patch resourceVersion test rejects stale
observations. This does not execute SQL, enable an app role, or claim readiness.
"""

from ipaddress import IPv4Address

OWNER_KEY = "bravo.dev/app-login-owner"
OWNER = "cutover-executor"
CONTROL_SECRET = "cutover-db-control"
TRUST_SECRET = "cutover-backend-ca"


def cluster_patch(cluster: dict, *, side: str) -> list[dict]:
    if side not in {"source", "target"}:
        raise ValueError("unknown cutover side")
    meta, spec = cluster["metadata"], cluster["spec"]
    if (cluster.get("kind") != "Cluster" or meta.get("name") != f"{side}-db"
            or meta.get("namespace") != f"{side}-dev"):
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


def pooler_trust_patch(deployment: dict, *, side: str) -> list[dict]:
    if side not in {"source", "target"}:
        raise ValueError("unknown cutover side")
    meta = deployment["metadata"]
    if (deployment.get("kind") != "Deployment" or meta.get("name") != f"{side}-pgbouncer"
            or meta.get("namespace") != f"{side}-dev" or deployment["spec"]["replicas"] != 1):
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


TARGET_HOST = "target-db-rw.target-dev.svc.cluster.local"
PROXY_GROUP = "bravo-source-pooler"


def target_route_patch(deployment: dict, parent: dict, route: dict) -> list[dict]:
    """Add the target TLS hostname without changing the current database target."""
    meta, spec = deployment["metadata"], deployment["spec"]
    if (deployment.get("kind") != "Deployment" or meta.get("name") != "source-pgbouncer"
            or meta.get("namespace") != "source-dev" or spec.get("replicas") != 1
            or spec.get("strategy", {}).get("type") != "Recreate"):
        raise ValueError("expected singleton source pooler")
    pod = spec["template"]["spec"]
    ca = [v for v in pod["volumes"] if v["name"] == "ca"]
    if len(ca) != 1 or ca[0].get("secret", {}).get("secretName") != "cutover-backend-ca":
        raise ValueError("source and target backend CA bundle must be prepared first")
    pm, rm = parent["metadata"], route["metadata"]
    if (parent.get("kind") != "Service" or pm.get("name") != "target-db-for-pooler"
            or pm.get("namespace") != "source-dev"
            or pm.get("annotations", {}).get("tailscale.com/proxy-group") != PROXY_GROUP
            or parent["spec"].get("type") != "ExternalName"):
        raise ValueError("expected dedicated source pooler route")
    labels = rm.get("labels", {})
    if (route.get("kind") != "Service" or rm.get("namespace") != "tailscale-system"
            or route["spec"].get("type") != "ClusterIP"
            or labels.get("tailscale.com/managed") != "true"
            or labels.get("tailscale.com/proxy-group") != PROXY_GROUP
            or labels.get("tailscale.com/parent-resource") != pm["name"]
            or labels.get("tailscale.com/parent-resource-ns") != pm["namespace"]
            or parent["spec"].get("externalName") != rm["name"] + ".tailscale-system.svc.cluster.local"):
        raise ValueError("generated Service does not belong to the dedicated route")
    if not any(c.get("type") == "TailscaleEgressSvcReady" and c.get("status") == "True"
               for c in parent.get("status", {}).get("conditions", [])):
        raise ValueError("route is not ready")
    ports = [p for p in route["spec"]["ports"] if p["port"] == 5432]
    if (len(ports) != 1 or ports[0].get("protocol") != "TCP"
            or type(ports[0].get("targetPort")) is not int
            or not 10000 <= ports[0]["targetPort"] <= 10999):
        raise ValueError("unexpected database proxy port")
    ip = IPv4Address(route["spec"]["clusterIP"])
    if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise ValueError("expected private ClusterIP")
    aliases = pod.get("hostAliases", [])
    current = [alias for alias in aliases if TARGET_HOST in alias["hostnames"]]
    if current and (len(current) != 1 or current[0]["ip"] != str(ip)):
        raise ValueError("existing target hostname mapping differs; review route replacement")
    patches = [{"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
    if not current:
        patches.append({"op": "add", "path": "/spec/template/spec/hostAliases",
                        "value": aliases + [{"ip": str(ip), "hostnames": [TARGET_HOST]}]})
    return patches
