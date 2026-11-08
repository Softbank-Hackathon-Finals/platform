"""The tailnet side of the pre-declared routes: a policy fragment derived from the desired states,
a check that a policy file allows exactly what the routes need, and a live check through Tailscale.

The fragment is deterministic: same desired states, same file. Each tailnet has its own policy, so
the fragment, the check and the live verify take one tailnet key (`default`, or a group's `tailnet`). It carries tag owners for each
device tag a module declares (egress groups, exposed devices), one grant per (group tag, destination tag,
port) and policy tests (accept the route, deny SSH to the same destination) so Tailscale itself refuses a
policy that breaks them.
"""

import ipaddress
import json
import subprocess

from .catalog import check_state, module
from .modules import DEFAULT_TAILNET, KUBERNETES_CAP, IacError, tailnet_of

TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _groups(states, tailnet=None):
    for state in states:
        for entry in check_state(state)["modules"]:
            if entry["id"] == "tailnet-egress":
                for group in entry["params"]["groups"]:
                    if tailnet is None or tailnet_of(group) == tailnet:
                        yield entry["params"]["owner_tag"], group


def _devices(states, tailnet=None):
    """(hostname, tag) of every device a tailnet-ingress module exposes."""
    for state in states:
        for entry in check_state(state)["modules"]:
            if entry["id"] == "tailnet-ingress":
                for device in entry["params"]["devices"]:
                    if tailnet is None or tailnet_of(device) == tailnet:
                        yield device["hostname"], device["tag"]


def _api_proxies(states, tailnet=None):
    """(hostname, tag) of every API server proxy an access module names."""
    for state in states:
        for entry in check_state(state)["modules"]:
            if entry["id"] in {"operator-access", "api-transport-access", "workload-api-access"} and (
                    tailnet is None or tailnet_of(entry["params"]) == tailnet):
                yield entry["params"]["proxy_hostname"], entry["params"]["proxy_tag"]


# The operator still reads the capability under its old URL key, so a grant under either key counts.
CAP_KEYS = (KUBERNETES_CAP, "https://" + KUBERNETES_CAP)


TAILNET_RANGES = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _may_be_proxy(dst: str, hosts: dict) -> bool:
    """``*``, a tailnet address or CIDR, or a host alias for one may be any declared proxy device, so it is
    checked as if it named every one of them. Tags, groups, users and subnet routes are not proxies."""
    if dst == "*":
        return True
    if dst.startswith(("tag:", "group:", "autogroup:")) or "@" in dst:
        return False
    try:
        network = ipaddress.ip_network(hosts.get(dst, dst), strict=False)
    except (TypeError, ValueError):
        return True  # a name the policy does not resolve: it cannot be ruled out
    return any(network.version == r.version and network.overlaps(r) for r in TAILNET_RANGES)


def _reaches_443(ips) -> bool:
    for ip in ips:
        port = ip.split(":", 1)[1] if ip.startswith(("tcp:", "udp:")) else ip
        if ip.startswith("udp:"):
            continue
        if port == "*" or port == "443":
            return True
        low, sep, high = port.partition("-")
        if sep and low.isdigit() and high.isdigit() and int(low) <= 443 <= int(high):
            return True
    return False


def _impersonated(app) -> list[str]:
    return [group for key in CAP_KEYS for rule in (app or {}).get(key) or [] if isinstance(rule, dict)
            for group in (rule.get("impersonate") or {}).get("groups") or []]


def fragment(states, tailnet=DEFAULT_TAILNET) -> dict:
    owners, grants, accepts, approvers = {}, set(), {}, {}
    for state in states:
        for entry in check_state(state)["modules"]:
            found = module(entry["id"], entry["version"])
            for g in found.grants(entry["params"]):
                if g.get("tailnet", DEFAULT_TAILNET) == tailnet:
                    app = json.dumps(g["app"], sort_keys=True) if "app" in g else ""
                    grants.add((g["src"][0], g["dst"][0], g["ip"][0] if "ip" in g else "", app))  # "": capability only
            for o in found.tag_owners(entry["params"]):
                if o["tailnet"] == tailnet and owners.setdefault(o["tag"], o["owner"]) != o["owner"]:
                    raise IacError("IAC_TAG_OWNER_CONFLICT")
            for a in found.auto_approvers(entry["params"]):
                if a["tailnet"] == tailnet:
                    approvers.setdefault(a["kind"], {}).setdefault(a["name"], set()).add(a["approver"])
    # Policy tests cover tagged route devices only. Human group/role membership lives outside
    # this repository; check_policy matches its grants without inventing a test user.
    for src, dst, ip, app in grants:
        if not app and src.startswith("tag:"):
            accepts.setdefault(src, set()).add(f"{dst}:{ip.removeprefix('tcp:')}")
    destinations = sorted({dst for src, dst, _, app in grants if not app and src.startswith("tag:")})
    # A new cluster's operator tag (tailnet-operator) has no grant of its own. Its devices get names at
    # run time that a repository scan cannot see, so the policy itself tests that the tag reaches no
    # granted destination: other environments' operators, databases, poolers and the collectors.
    granted = sorted({f"{dst}:{ip.removeprefix('tcp:')}" for _, dst, ip, app in grants if not app})
    operators = sorted({entry["params"]["tag"] for state in states for entry in check_state(state)["modules"]
                        if entry["id"] == "tailnet-operator" and tailnet_of(entry["params"]) == tailnet})
    return {
        "tagOwners": {tag: [owners[tag]] for tag in sorted(owners)},
        "grants": [{"src": [s], "dst": [d], **({"ip": [i]} if i else {}), **({"app": json.loads(a)} if a else {})}
                   for s, d, i, a in sorted(grants)],
        # Each route device is also denied every other declared dst:port it was not granted, so a hand edit that
        # opens a declared device more widely (e.g. `*` toward tag:bravo-findings:8443) fails the policy tests.
        "tests": [{"src": src, "accept": sorted(accepts[src]),
                   "deny": [f"{dst}:22" for dst in destinations]
                   + [d for d in granted if d not in accepts[src]]} for src in sorted(accepts)]
                 + [{"src": tag, "accept": [], "deny": granted} for tag in operators if granted],
        # Only when a module needs one (a kube-apiserver ProxyGroup advertises a Tailscale Service).
        **({"autoApprovers": {kind: {name: sorted(tags) for name, tags in sorted(table.items())}
                              for kind, table in sorted(approvers.items())}} if approvers else {}),
    }


def check_policy(policy, states, tailnet=DEFAULT_TAILNET) -> list[str]:
    """Codes for what the policy lacks or adds beyond the desired state (empty: it allows every declared
    route, owns every group tag, and gives no undeclared access to a declared API proxy)."""
    if type(policy) is not dict:
        raise IacError("IAC_POLICY_INVALID")
    want = fragment(states, tailnet)
    missing = []
    owners = policy.get("tagOwners") or {}
    for tag, owner in want["tagOwners"].items():
        if owners.get(tag) != owner:
            missing.append(f"TAG_OWNER:{tag}")
    have = policy.get("autoApprovers") if isinstance(policy.get("autoApprovers"), dict) else {}
    for kind, table in (want.get("autoApprovers") or {}).items():
        for name, tags in table.items():
            missing += [f"AUTO_APPROVER:{name}:{tag}" for tag in tags if tag not in ((have.get(kind) or {}).get(name) or [])]
    for grant in want["grants"]:
        src, dst, ips = grant["src"][0], grant["dst"][0], grant.get("ip", [])
        if not any(src in g.get("src", []) and dst in g.get("dst", [])
                   and all(ip in g.get("ip", []) or "*" in g.get("ip", []) for ip in ips)
                   and ("app" not in grant or g.get("app") == grant["app"]) for g in policy.get("grants") or []):
            missing.append(f"GRANT:{src}->{dst}" + "".join(f":{ip}" for ip in ips) + ("+app" if "app" in grant else ""))
    # What the desired state does not declare toward a declared API proxy would let a policy edit alone
    # hand out cluster access: a capability grant (the proxy may impersonate any group), or plain reach
    # to its 443 from another source (a transport proxy passes the caller's own credential through).
    # `*`, addresses and host aliases count as every proxy; the ports of a declared grant may be wider.
    declared = {(g["src"][0], g["dst"][0], json.dumps(g["app"], sort_keys=True)) for g in want["grants"] if "app" in g}
    reach = {(g["src"][0], g["dst"][0]) for g in want["grants"]}
    proxies = {tag for _, tag in _api_proxies(states, tailnet)}
    hosts = policy.get("hosts") if isinstance(policy.get("hosts"), dict) else {}
    for g in policy.get("grants") or []:
        dsts, app = set(g.get("dst", [])), g.get("app") or {}
        if any(group.startswith("system:") for group in _impersonated(app)):
            missing += [f"SYSTEM_IMPERSONATION:{src}" for src in g.get("src", [])]  # whatever it reaches
        hit = proxies if any(_may_be_proxy(d, hosts) for d in dsts) else proxies & dsts
        capability = any(key in app for key in CAP_KEYS)
        for src in g.get("src", []):
            for dst in sorted(hit):
                if capability and (src, dst, json.dumps(app, sort_keys=True)) not in declared:
                    missing.append(f"UNDECLARED_GRANT:{src}->{dst}+app")
                elif not capability and _reaches_443(g.get("ip", [])) and (src, dst) not in reach:
                    missing.append(f"UNDECLARED_REACH:{src}->{dst}:443")
    tests = policy.get("tests") or []
    for test in want["tests"]:
        have = {a for t in tests if t.get("src") == test["src"] for a in t.get("accept", [])}
        missing += [f"TEST:{test['src']}->{a}" for a in test["accept"] if a not in have]
    return missing


def status(raw=None) -> dict:
    """hostname -> {"tags", "online"} of the peers this device sees (from `tailscale status --json`)."""
    if raw is None:
        raw = subprocess.run([TAILSCALE, "status", "--json"], capture_output=True, text=True, timeout=20,
                             check=True).stdout
    doc = json.loads(raw)
    if doc.get("BackendState") != "Running":
        raise IacError("IAC_TAILNET_NOT_RUNNING")
    return {p.get("HostName"): {"tags": sorted(p.get("Tags") or []), "online": p.get("Online") is True}
            for p in (doc.get("Peer") or {}).values()}


def verify(states, peers, tailnet=None) -> list[dict]:
    """Each declared destination and egress group as Tailscale sees it from this device.

    OK: present, online, carrying the declared tag. NOT_VISIBLE: this device's grants do not show it
    (or it does not exist); that is not a failure of the route itself. OFFLINE and TAG_MISMATCH are.
    """
    rows = []

    def judge(kind, host, tag, match):
        if match is None:
            return {"kind": kind, "host": host, "tag": tag, "result": "NOT_VISIBLE"}
        result = ("TAG_MISMATCH" if tag not in match["tags"] else "OFFLINE" if not match["online"] else "OK")
        return {"kind": kind, "host": host, "tag": tag, "result": result, "seen_tags": match["tags"]}

    for _, group in _groups(states, tailnet):
        replicas = [p for h, p in peers.items() if isinstance(h, str) and h.startswith(group["name"] + "-")]
        rows.append(judge("proxy-group", group["name"], group["tag"], replicas[0] if replicas else None))
        for route in group["routes"]:
            rows.append(judge("destination", route["destination"], route["destination_tag"],
                              peers.get(route["destination"])))
    for host, tag in _devices(states, tailnet):
        rows.append(judge("destination", host, tag, peers.get(host)))
    for host, tag in _api_proxies(states, tailnet):
        rows.append(judge("api-proxy", host, tag, peers.get(host)))
    unique = {json.dumps(r, sort_keys=True) for r in rows}
    return [json.loads(r) for r in sorted(unique)]
