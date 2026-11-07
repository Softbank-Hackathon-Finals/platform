"""IaC modules: versioned, declarative units of destination configuration (T44).

A module checks its params and renders the desired *shape* of the resources it owns. A shape
never holds a secret value: credentials are injected by the applier at apply time and a plan
only names them. Rendering is a pure function, so the same params always give the same plan.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass


class IacError(ValueError):
    """A fixed code. It never carries input text, paths or values."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Module:
    id: str
    version: str
    description: str
    params: dict  # parameter schema shown in the catalog (JSON Schema subset)
    check: Callable[[dict], dict]
    render: Callable[[dict], list[dict]]
    exposure: Callable[[dict], dict]  # what callers may reach ("anonymous", "reach"), for widening checks
    grants: Callable[[dict], list[dict]] = lambda params: []  # tailnet grants the module needs
    # tags the module puts on tailnet devices: [{"tailnet", "tag", "owner"}], for the policy's tagOwners
    tag_owners: Callable[[dict], list[dict]] = lambda params: []


# --- target-object-exposure v1 ---------------------------------------------------------------
# The SeaweedFS identity file (s3.json) of the target attachments bucket. Same identities and
# actions as T9 infra/envs/target-workloads/object_exposure.py; tests pin the equality.
BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{3,63}")
CLIENT_ACTIONS = ("Read", "Write", "List", "Tagging")
BOOTSTRAP_ACTIONS = ("Admin",) + CLIENT_ACTIONS
ANONYMOUS_ACTIONS = ("Read",)
TARGET_NAMESPACE = "target-dev"


def _exposure_check(params: dict) -> dict:
    if (type(params) is not dict or not set(params) <= {"state", "bucket", "demo_run_id"}
            or {"state", "bucket"} - set(params)):
        raise IacError("IAC_PARAMS_INVALID")
    state, bucket, run = params["state"], params["bucket"], params.get("demo_run_id")
    if (state not in ("closed", "open-demo") or not isinstance(bucket, str)
            or not BUCKET_RE.fullmatch(bucket) or ".." in bucket):
        raise IacError("IAC_PARAMS_INVALID")
    # open-demo is the demo's planted defect and always names its run; closed never does.
    if (state == "open-demo") != (run is not None) or (
            run is not None and (not isinstance(run, str) or not RUN_ID_RE.fullmatch(run))):
        raise IacError("IAC_PARAMS_INVALID")
    return dict(params)


def _exposure_identities(params: dict) -> list[dict]:
    bucket = params["bucket"]
    identities = [
        {"name": "bootstrap", "actions": [f"{a}:{bucket}" for a in BOOTSTRAP_ACTIONS]},
        {"name": "client", "actions": [f"{a}:{bucket}" for a in CLIENT_ACTIONS]},
    ]
    if params["state"] == "open-demo":
        identities.append({"name": "anonymous", "actions": [f"{a}:{bucket}" for a in ANONYMOUS_ACTIONS]})
    return identities


def _exposure_render(params: dict) -> list[dict]:
    return [{
        "kind": "Secret", "namespace": TARGET_NAMESPACE, "name": "target-seaweedfs-auth", "key": "s3.json",
        "shape": {"identities": _exposure_identities(params)},
        "secret_inputs": ["bootstrap", "client"],  # credentials: injected by the applier, never in Git
        "restart_on_change": ["Deployment/target-dev/target-seaweedfs"],
    }]


def _exposure_exposure(params: dict) -> dict:
    anonymous = [i for i in _exposure_identities(params) if i["name"] == "anonymous"]
    return {"anonymous": sorted(anonymous[0]["actions"]) if anonymous else [],
            "checks": [{"control": "IAM-01.1", "anonymous_get": "ALLOWED" if anonymous else "DENIED"}]}


TARGET_OBJECT_EXPOSURE = Module(
    id="target-object-exposure",
    version="1",
    description="Target attachments bucket identities (SeaweedFS s3.json). closed: app and bootstrap only; "
                "open-demo: also anonymous Read, the demo's planted defect.",
    params={"type": "object", "additionalProperties": False, "required": ["state", "bucket"],
            "properties": {"state": {"enum": ["closed", "open-demo"]},
                           "bucket": {"type": "string", "pattern": BUCKET_RE.pattern},
                           "demo_run_id": {"type": "string", "pattern": RUN_ID_RE.pattern,
                                           "description": "required for open-demo only"}}},
    check=_exposure_check,
    render=_exposure_render,
    exposure=_exposure_exposure,
)


# --- tailnet-egress v1 ------------------------------------------------------------------------
# Pre-declared Tailscale egress routes of one cluster: per group one egress ProxyGroup (its own
# tag), ExternalName Services to tailnet hosts, and NetworkPolicies bound to the group's proxy
# Pods by selector. Tailscale 1.104 maps each route to a proxy Pod port in [10000, 11000); the
# policies open that range to the group's own Pods only, so nothing has to be observed after
# apply. Which destination and port a group may reach is decided by the tailnet grants this
# module derives, not by the cluster policy. The tailnet suffix is not in Git: "{tailnet}", or
# "{tailnet:<key>}" for a group that joins another tailnet (its own `tailnet` key, e.g. workload).
NAME_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
TAG_RE = re.compile(r"tag:[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
LABEL_KEY_RE = re.compile(r"(?:[a-z0-9.-]{1,253}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
LABEL_VALUE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?")
PROXY_PORTS = (10000, 10999)  # Tailscale operator egress port allocation, [10000, 11000)
OPERATOR_HEALTH_PORT = 9002
DEFAULT_TAILNET = "default"
TAILSCALE_NAMESPACE = "tailscale-system"


def _name(value) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise IacError("IAC_PARAMS_INVALID")
    return value


def _tag(value) -> str:
    if not isinstance(value, str) or not TAG_RE.fullmatch(value):
        raise IacError("IAC_PARAMS_INVALID")
    return value


def _exact(value, required, optional=()) -> dict:
    if type(value) is not dict or not set(required) <= set(value) <= set(required) | set(optional):
        raise IacError("IAC_PARAMS_INVALID")
    return value


def _labels(value) -> dict:
    if (type(value) is not dict or not 1 <= len(value) <= 8
            or not all(isinstance(k, str) and LABEL_KEY_RE.fullmatch(k) and isinstance(v, str)
                       and LABEL_VALUE_RE.fullmatch(v) for k, v in value.items())):
        raise IacError("IAC_PARAMS_INVALID")
    return dict(sorted(value.items()))


def _egress_check(params: dict) -> dict:
    _exact(params, ("owner_tag", "groups"))
    owner, groups = _tag(params["owner_tag"]), params["groups"]
    if type(groups) is not list or not 1 <= len(groups) <= 8:
        raise IacError("IAC_PARAMS_INVALID")
    seen_groups, seen_services, checked = set(), set(), []
    for group in groups:
        _exact(group, ("name", "tag", "clients", "routes"), ("proxy_class", "tailnet", "client_policy"))
        name, tag = _name(group["name"]), _tag(group["tag"])
        clients, routes = group["clients"], group["routes"]
        if (name in seen_groups or tag == owner or type(clients) is not list or not 1 <= len(clients) <= 8
                or type(routes) is not list or not 1 <= len(routes) <= 16):
            raise IacError("IAC_PARAMS_INVALID")
        seen_groups.add(name)
        out = {"name": name, "tag": tag,
               "clients": [{"namespace": _name(_exact(c, ("namespace", "labels"))["namespace"]),
                            "labels": _labels(c["labels"])} for c in clients],
               "routes": []}
        if "proxy_class" in group:
            out["proxy_class"] = _name(group["proxy_class"])
        if "tailnet" in group:
            if _name(group["tailnet"]) == DEFAULT_TAILNET:
                raise IacError("IAC_PARAMS_INVALID")
            out["tailnet"] = group["tailnet"]
        if "client_policy" in group:
            # false: the clients keep their own egress (e.g. CNPG instances, whose backups and
            # operator calls this module does not know); only the proxy side is restricted to them.
            if group["client_policy"] is not False:
                raise IacError("IAC_PARAMS_INVALID")
            out["client_policy"] = False
        for route in routes:
            _exact(route, ("service", "namespace", "destination", "destination_tag", "port", "port_name"))
            key = (_name(route["namespace"]), _name(route["service"]))
            port = route["port"]
            if key in seen_services or type(port) is not int or not 1 <= port <= 65535:
                raise IacError("IAC_PARAMS_INVALID")
            seen_services.add(key)
            out["routes"].append({"service": key[1], "namespace": key[0], "destination": _name(route["destination"]),
                                  "destination_tag": _tag(route["destination_tag"]), "port": port,
                                  "port_name": _name(route["port_name"])})
        checked.append(out)
    return {"owner_tag": owner, "groups": checked}


def _proxy_peer(group: str) -> dict:
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": TAILSCALE_NAMESPACE}},
            "podSelector": {"matchLabels": {"tailscale.com/parent-resource": group,
                                            "tailscale.com/parent-resource-type": "proxygroup"}}}


def _client_peer(client: dict) -> dict:
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": client["namespace"]}},
            "podSelector": {"matchLabels": client["labels"]}}


def _suffix(group: dict) -> str:
    return "{tailnet:" + group["tailnet"] + "}" if "tailnet" in group else "{tailnet}"


def tailnet_of(group: dict) -> str:
    return group.get("tailnet", DEFAULT_TAILNET)


def _proxy_range() -> dict:
    return {"protocol": "TCP", "port": PROXY_PORTS[0], "endPort": PROXY_PORTS[1]}


def _egress_render(params: dict) -> list[dict]:
    out = []
    for group in params["groups"]:
        name = group["name"]
        spec = {"type": "egress", "replicas": 1, "tags": [group["tag"]]}
        if "proxy_class" in group:
            spec["proxyClass"] = group["proxy_class"]
        out.append({"kind": "ProxyGroup", "namespace": "-", "name": name, "shape": spec})
        for route in group["routes"]:
            out.append({"kind": "Service", "namespace": route["namespace"], "name": route["service"], "shape": {
                "annotations": {"tailscale.com/tailnet-fqdn": route["destination"] + "." + _suffix(group),
                                "tailscale.com/proxy-group": name},
                "type": "ExternalName", "externalName": "placeholder",
                "ports": [{"name": route["port_name"], "port": route["port"], "protocol": "TCP"}]}})
        for namespace in sorted({c["namespace"] for c in group["clients"]} if group.get("client_policy", True) else ()):
            selectors = [c["labels"] for c in group["clients"] if c["namespace"] == namespace]
            for index, labels in enumerate(selectors):
                out.append({"kind": "NetworkPolicy", "namespace": namespace,
                            "name": f"{name}-client" + (f"-{index}" if index else ""), "shape": {
                                "podSelector": {"matchLabels": labels}, "policyTypes": ["Egress"],
                                "egress": [
                                    {"to": [{"namespaceSelector": {"matchLabels": {
                                        "kubernetes.io/metadata.name": "kube-system"}}}],
                                     "ports": [{"protocol": p, "port": 53} for p in ("UDP", "TCP")]},
                                    {"to": [_proxy_peer(name)], "ports": [_proxy_range()]}]}})
        out.append({"kind": "NetworkPolicy", "namespace": TAILSCALE_NAMESPACE, "name": f"{name}-ingress", "shape": {
            "podSelector": {"matchLabels": _proxy_peer(name)["podSelector"]["matchLabels"]},
            "policyTypes": ["Ingress"],
            "ingress": [
                {"from": [{"podSelector": {"matchLabels": {"app": "operator"}}}],
                 "ports": [{"protocol": "TCP", "port": OPERATOR_HEALTH_PORT}]},
                {"from": [_client_peer(c) for c in group["clients"]], "ports": [_proxy_range()]}]}})
    return out


def _egress_grants(params: dict) -> list[dict]:
    pairs = sorted({(tailnet_of(g), g["tag"], r["destination_tag"], r["port"])
                    for g in params["groups"] for r in g["routes"]})
    return [{"tailnet": net, "src": [src], "dst": [dst], "ip": [f"tcp:{port}"]} for net, src, dst, port in pairs]


def _client(c: dict) -> str:
    return f'{c["namespace"]}/{",".join(f"{k}={v}" for k, v in c["labels"].items())}'


def _egress_exposure(params: dict) -> dict:
    # The tailnet is part of the destination: the same host name on another tailnet is another device.
    reach = {f'{_client(c)} -> {r["destination"]}@{tailnet_of(g)}({r["destination_tag"]}):{r["port"]}'
             for g in params["groups"] for c in g["clients"] for r in g["routes"]}
    # Dropping a group's client policy lets those clients reach anything else: a widening.
    reach |= {f"{_client(c)} -> * (egress not isolated by {g['name']})"
              for g in params["groups"] if g.get("client_policy") is False for c in g["clients"]}
    return {"anonymous": [], "reach": sorted(reach), "checks": []}


def _egress_owners(params: dict) -> list[dict]:
    return [{"tailnet": tailnet_of(g), "tag": g["tag"], "owner": params["owner_tag"]} for g in params["groups"]]


TAILNET_EGRESS = Module(
    id="tailnet-egress",
    version="1",
    description="Pre-declared Tailscale egress routes of one cluster: per group an egress ProxyGroup with its own "
                "tag, ExternalName Services to tailnet hosts and NetworkPolicies bound to the group's proxy Pods "
                "(port range, nothing observed after apply). Destination and port are gated by the derived "
                "tailnet grants.",
    params={"type": "object", "additionalProperties": False, "required": ["owner_tag", "groups"],
            "properties": {"owner_tag": {"type": "string", "pattern": TAG_RE.pattern},
                           "groups": {"type": "array", "minItems": 1, "maxItems": 8}}},
    check=_egress_check,
    render=_egress_render,
    exposure=_egress_exposure,
    grants=_egress_grants,
    tag_owners=_egress_owners,
)


# --- tailnet-ingress v1 -----------------------------------------------------------------------
# In-cluster workloads exposed to the tailnet as their own devices: one Service per device with
# the Tailscale operator's expose annotations (hostname, tag), a selector and ports. Who may reach
# a device is decided by the consumers' egress grants (tailnet-egress), not here; this module owns
# the device tag (tagOwners: the cluster operator's tag) and what the device forwards to.
def _ingress_check(params: dict) -> dict:
    _exact(params, ("owner_tag", "devices"))
    owner, devices = _tag(params["owner_tag"]), params["devices"]
    if type(devices) is not list or not 1 <= len(devices) <= 16:
        raise IacError("IAC_PARAMS_INVALID")
    seen_services, seen_hosts, checked = set(), set(), []
    for device in devices:
        _exact(device, ("service", "namespace", "hostname", "tag", "selector", "ports"), ("tailnet",))
        key = (_name(device["namespace"]), _name(device["service"]))
        host, tag, ports = _name(device["hostname"]), _tag(device["tag"]), device["ports"]
        if key in seen_services or host in seen_hosts or tag == owner or type(ports) is not list or not 1 <= len(ports) <= 4:
            raise IacError("IAC_PARAMS_INVALID")
        seen_services.add(key)
        seen_hosts.add(host)
        out = {"service": key[1], "namespace": key[0], "hostname": host, "tag": tag,
               "selector": _labels(device["selector"]), "ports": []}
        if "tailnet" in device:
            if _name(device["tailnet"]) == DEFAULT_TAILNET:
                raise IacError("IAC_PARAMS_INVALID")
            out["tailnet"] = device["tailnet"]
        for port in ports:
            _exact(port, ("name", "port", "target_port"))
            if not all(type(port[k]) is int and 1 <= port[k] <= 65535 for k in ("port", "target_port")):
                raise IacError("IAC_PARAMS_INVALID")
            out["ports"].append({"name": _name(port["name"]), "port": port["port"], "target_port": port["target_port"]})
        if len({p["port"] for p in out["ports"]}) != len(out["ports"]):
            raise IacError("IAC_PARAMS_INVALID")
        checked.append(out)
    return {"owner_tag": owner, "devices": checked}


def _ingress_render(params: dict) -> list[dict]:
    return [{"kind": "Service", "namespace": d["namespace"], "name": d["service"], "shape": {
        "annotations": {"tailscale.com/expose": "true", "tailscale.com/hostname": d["hostname"],
                        "tailscale.com/tags": d["tag"]},
        "selector": d["selector"],
        "ports": [{"name": p["name"], "port": p["port"], "targetPort": p["target_port"], "protocol": "TCP"}
                  for p in d["ports"]]}} for d in params["devices"]]


def _ingress_exposure(params: dict) -> dict:
    # What the tailnet can reach in this cluster: a new device, port or wider selector is a widening.
    reach = sorted({f'tailnet {d["hostname"]}@{tailnet_of(d)}({d["tag"]}):{p["port"]} -> '
                    f'{d["namespace"]}/{",".join(f"{k}={v}" for k, v in d["selector"].items())}:{p["target_port"]}'
                    for d in params["devices"] for p in d["ports"]})
    return {"anonymous": [], "reach": reach, "checks": []}


def _ingress_owners(params: dict) -> list[dict]:
    return [{"tailnet": tailnet_of(d), "tag": d["tag"], "owner": params["owner_tag"]} for d in params["devices"]]


TAILNET_INGRESS = Module(
    id="tailnet-ingress",
    version="1",
    description="In-cluster workloads exposed to the tailnet as their own devices (Tailscale operator expose "
                "Services: hostname, tag, selector, ports). Who may reach them is gated by the consumers' "
                "tailnet-egress grants.",
    params={"type": "object", "additionalProperties": False, "required": ["owner_tag", "devices"],
            "properties": {"owner_tag": {"type": "string", "pattern": TAG_RE.pattern},
                           "devices": {"type": "array", "minItems": 1, "maxItems": 16}}},
    check=_ingress_check,
    render=_ingress_render,
    exposure=_ingress_exposure,
    tag_owners=_ingress_owners,
)


# --- operator-access v1 -----------------------------------------------------------------------
# People reach a cluster's API only through that cluster's Tailscale API server proxy (operator auth
# mode: apiServerProxyConfig.mode "true"). The tailnet grant lets a tailnet group reach the proxy and
# names the Kubernetes group the proxy impersonates; a ClusterRoleBinding gives that group its role.
# Who is in the tailnet group lives in the tailnet policy, never in this public repository.
K8S_GROUP_RE = re.compile(r"[a-z][a-z0-9-]{0,30}:[a-z][a-z0-9-]{0,62}")
# A tailnet group of the policy, or the tailnet role autogroups (admins, the owner). Never autogroup:member.
TAILNET_GROUP_RE = re.compile(r"group:[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?|autogroup:(?:admin|owner)")
KUBERNETES_CAP = "tailscale.com/cap/kubernetes"
API_PROXY_PORT = "tcp:443"


def _access_check(params: dict) -> dict:
    _exact(params, ("proxy_hostname", "proxy_tag", "bindings"), ("tailnet",))
    bindings = params["bindings"]
    if type(bindings) is not list or not 1 <= len(bindings) <= 4:
        raise IacError("IAC_PARAMS_INVALID")
    out = {"proxy_hostname": _name(params["proxy_hostname"]), "proxy_tag": _tag(params["proxy_tag"]), "bindings": []}
    if "tailnet" in params:
        if _name(params["tailnet"]) == DEFAULT_TAILNET:
            raise IacError("IAC_PARAMS_INVALID")
        out["tailnet"] = params["tailnet"]
    for binding in bindings:
        _exact(binding, ("name", "group", "cluster_role", "tailnet_groups"))
        name, group, sources = _name(binding["name"]), binding["group"], binding["tailnet_groups"]
        # system:* groups (system:masters above all) are never impersonated: RBAC decides every right.
        if (not isinstance(group, str) or not K8S_GROUP_RE.fullmatch(group) or group.startswith("system:")
                or type(sources) is not list or not 1 <= len(sources) <= 4 or len(set(map(str, sources))) != len(sources)
                or not all(isinstance(s, str) and TAILNET_GROUP_RE.fullmatch(s) for s in sources)
                or any(b["name"] == name or b["group"] == group for b in out["bindings"])):
            raise IacError("IAC_PARAMS_INVALID")
        role = _name(binding["cluster_role"])
        # roleRef is immutable: a role change must be a new binding, so the name is the group and the role.
        if name != group.replace(":", "-") + "-" + role:
            raise IacError("IAC_PARAMS_INVALID")
        out["bindings"].append({"name": name, "group": group, "cluster_role": role,
                                "tailnet_groups": sorted(sources)})
    return out


def _access_render(params: dict) -> list[dict]:
    # A binding grants its role; whoever creates it needs that role (RBAC escalation). The network
    # Application never gets it: a cluster administrator applies these (chart.held_objects).
    return [{"kind": "ClusterRoleBinding", "namespace": "-", "name": b["name"], "applier": "cluster-admin", "shape": {
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": b["cluster_role"]},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": b["group"]}]}}
        for b in params["bindings"]]


def _access_grants(params: dict) -> list[dict]:
    # One grant both opens the proxy port and tells the proxy which Kubernetes group to impersonate.
    return [{"tailnet": tailnet_of(params), "src": [src], "dst": [params["proxy_tag"]],
             "ip": [API_PROXY_PORT], "app": {KUBERNETES_CAP: [{"impersonate": {"groups": [b["group"]]}}]}}
            for b in params["bindings"] for src in b["tailnet_groups"]]


def _access_exposure(params: dict) -> dict:
    # A new binding, another role or another tailnet group is a widening; removing a binding is not.
    reach = sorted(f'{src}@{tailnet_of(params)} -> {params["proxy_hostname"]}({params["proxy_tag"]})'
                   f' as {b["group"]}:{b["cluster_role"]}' for b in params["bindings"] for src in b["tailnet_groups"])
    return {"anonymous": [], "reach": reach, "checks": []}


OPERATOR_ACCESS = Module(
    id="operator-access",
    version="1",
    description="People reach the cluster API through its Tailscale API server proxy: per binding, a "
                "ClusterRoleBinding of one Kubernetes group and the tailnet grant that lets a tailnet group "
                "reach the proxy and be impersonated as that group.",
    params={"type": "object", "additionalProperties": False, "required": ["proxy_hostname", "proxy_tag", "bindings"],
            "properties": {"proxy_hostname": {"type": "string", "pattern": NAME_RE.pattern},
                           "proxy_tag": {"type": "string", "pattern": TAG_RE.pattern},
                           "tailnet": {"type": "string", "pattern": NAME_RE.pattern},
                           "bindings": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                               "type": "object", "additionalProperties": False,
                               "required": ["name", "group", "cluster_role", "tailnet_groups"],
                               "properties": {"name": {"type": "string", "pattern": NAME_RE.pattern},
                                              "group": {"type": "string", "pattern": K8S_GROUP_RE.pattern},
                                              "cluster_role": {"type": "string", "pattern": NAME_RE.pattern},
                                              "tailnet_groups": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                                                  "type": "string", "pattern": TAILNET_GROUP_RE.pattern}}}}}}},
    check=_access_check,
    render=_access_render,
    exposure=_access_exposure,
    grants=_access_grants,
)
