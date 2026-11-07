"""The tailnet side of the pre-declared routes: a policy fragment derived from the desired states,
a check that a policy file allows exactly what the routes need, and a live check through Tailscale.

The fragment is deterministic: same desired states, same file. Each tailnet has its own policy, so
the fragment, the check and the live verify take one tailnet key (`default`, or a group's `tailnet`). It carries tag owners for each
device tag a module declares (egress groups, exposed devices), one grant per (group tag, destination tag,
port) and policy tests (accept the route, deny SSH to the same destination) so Tailscale itself refuses a
policy that breaks them.
"""

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
    """(hostname, tag) of every API server proxy an operator-access module names."""
    for state in states:
        for entry in check_state(state)["modules"]:
            if entry["id"] == "operator-access" and (tailnet is None or tailnet_of(entry["params"]) == tailnet):
                yield entry["params"]["proxy_hostname"], entry["params"]["proxy_tag"]


def fragment(states, tailnet=DEFAULT_TAILNET) -> dict:
    owners, grants, accepts = {}, set(), {}
    for state in states:
        for entry in check_state(state)["modules"]:
            found = module(entry["id"], entry["version"])
            for g in found.grants(entry["params"]):
                if g.get("tailnet", DEFAULT_TAILNET) == tailnet:
                    app = json.dumps(g["app"], sort_keys=True) if "app" in g else ""
                    grants.add((g["src"][0], g["dst"][0], g["ip"][0], app))
            for o in found.tag_owners(entry["params"]):
                if o["tailnet"] == tailnet and owners.setdefault(o["tag"], o["owner"]) != o["owner"]:
                    raise IacError("IAC_TAG_OWNER_CONFLICT")
    # Policy tests cover route grants only: a grant with an app capability (API server proxy access)
    # names a tailnet group whose members live outside this repository; check_policy matches it instead.
    for src, dst, ip, app in grants:
        if not app:
            accepts.setdefault(src, set()).add(f"{dst}:{ip.removeprefix('tcp:')}")
    destinations = sorted({dst for _, dst, _, app in grants if not app})
    return {
        "tagOwners": {tag: [owners[tag]] for tag in sorted(owners)},
        "grants": [{"src": [s], "dst": [d], "ip": [i], **({"app": json.loads(a)} if a else {})}
                   for s, d, i, a in sorted(grants)],
        "tests": [{"src": src, "accept": sorted(accepts[src]),
                   "deny": [f"{dst}:22" for dst in destinations]} for src in sorted(accepts)],
    }


def check_policy(policy, states, tailnet=DEFAULT_TAILNET) -> list[str]:
    """Codes for what the policy lacks (empty: it allows every declared route and owns every group tag)."""
    if type(policy) is not dict:
        raise IacError("IAC_POLICY_INVALID")
    want = fragment(states, tailnet)
    missing = []
    owners = policy.get("tagOwners") or {}
    for tag, owner in want["tagOwners"].items():
        if owners.get(tag) != owner:
            missing.append(f"TAG_OWNER:{tag}")
    for grant in want["grants"]:
        src, dst, ip = grant["src"][0], grant["dst"][0], grant["ip"][0]
        if not any(src in g.get("src", []) and dst in g.get("dst", []) and (ip in g.get("ip", []) or "*" in g.get("ip", []))
                   and ("app" not in grant or g.get("app") == grant["app"]) for g in policy.get("grants") or []):
            missing.append(f"GRANT:{src}->{dst}:{ip}" + ("+app" if "app" in grant else ""))
    # A capability grant to a declared API proxy that the desired state does not declare would let a
    # policy edit alone hand out cluster rights (the proxy may impersonate any group): report it too.
    declared = {json.dumps(g, sort_keys=True) for g in want["grants"] if "app" in g}
    proxies = {d for g in want["grants"] if "app" in g for d in g["dst"]}
    for g in policy.get("grants") or []:
        if KUBERNETES_CAP in (g.get("app") or {}) and proxies & set(g.get("dst", [])):
            for src in g.get("src", []):
                for dst in sorted(proxies & set(g["dst"])):
                    one = {"src": [src], "dst": [dst], "ip": g.get("ip", []), "app": g["app"]}
                    if json.dumps(one, sort_keys=True) not in declared:
                        missing.append(f"UNDECLARED_GRANT:{src}->{dst}+app")
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
