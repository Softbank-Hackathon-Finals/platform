"""Render a guarded target hostname patch for the source PgBouncer; no apply or SQL."""

from ipaddress import IPv4Address

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
