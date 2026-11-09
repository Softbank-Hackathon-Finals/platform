#!/usr/bin/env python3
"""Read-only check of the database routes on the tailnet: path taken, MTU, and ACL allow/deny.

Runs read commands inside the role-specific ProxyGroup Pods with `kubectl exec`. TCP probes connect
and close without sending bytes. No tailnet policy, node setting or database is changed. A value it
cannot read is UNKNOWN and never counts as PASS.

How to run and read the result: docs/operations/t30-tailnet-path-check.md
"""
import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

import network

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
EXIT_CODES = {PASS: 0, FAIL: 1, UNKNOWN: 2}
DIRECT, DERP, PEER_RELAY = "direct", "derp", "peer_relay"
CONNECTED, REFUSED, TIMEOUT, UNREAD, NO_PEER = "connected", "refused", "timeout", "unread", "no_tailnet_peer"

NAMESPACE = "tailscale-system"
CONTAINER = "tailscale"
ALLOWED_PORT = 5432
WRONG_PORT = 22  # The tailnet fragment's deny tests use the same port.
PONG_COUNT = 3
COMMAND_TIMEOUT_S = 30
# containerboot starts tailscaled on this socket; the CLI's own default is a different path.
SOCKET = "--socket=/tmp/tailscaled.sock"
MTU_FILE = "/sys/class/net/tailscale0/mtu"
MIN_INTERFACE_MTU = 576
BASELINE_PAYLOAD = 56
# (family, ping flag, IP + ICMP header bytes). See measure_mtu for why both are tried.
ICMP_FAMILIES = (("ipv4", "-4", 28), ("ipv6", "-6", 48))

Output = namedtuple("Output", "code stdout stderr")
Pong = namedtuple("Pong", "kind region ms")

_ADDRESS = r"[0-9a-fA-F.:]+"
READ_ONLY_POD_COMMANDS = tuple(re.compile(pattern) for pattern in (
    rf"tailscale {SOCKET} status --json",
    rf"tailscale {SOCKET} ping --c={PONG_COUNT} --timeout=3s --until-direct=false {_ADDRESS}",
    re.escape(f"cat {MTU_FILE}"),
    rf"ping -[46] -M do -c 3 -i 0\.2 -W 2 -s \d+ {_ADDRESS}",
    rf"nc -vv -z -w 3 {_ADDRESS} \d+",
))

# Anchored to the shape of `nc`'s own lines. kubectl's errors also say "connection refused" and
# "timed out", and reading those as probe results would turn an API failure into a denial.
NC_OPEN = re.compile(r"^\S+ \(\S+\) open$", re.MULTILINE)
NC_REFUSED = re.compile(r"^nc: \S+ \(\S+\): Connection refused$", re.MULTILINE)
NC_TIMEOUT = re.compile(r"^nc: \S+ \(\S+\): (?:Connection|Operation) timed out$", re.MULTILINE)
# iputils' wording. BusyBox says "packets received" and has no -M, so its output is not a DF result.
PING_SUMMARY = re.compile(r"^\d+ packets transmitted, (\d+) received", re.MULTILINE)
PING_RTT = re.compile(r"^rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", re.MULTILINE)
PONG = re.compile(r"^pong from .+ via (\S+) in (\d+(?:\.\d+)?)(ms|s)$", re.MULTILINE)
PONG_DERP = re.compile(r"DERP\(([a-z0-9-]{1,32})\)")
PONG_PEER_RELAY = re.compile(r"peer-relay\(.+\)")
PONG_DIRECT = re.compile(r"(?:\[[0-9a-fA-F:.]+\]|[0-9.]+):\d+")


class Unread(Exception):
    """A value could not be read. Its message is a fixed reason code, never command output."""


class Kube:
    """kubectl limited to `get` and the read commands above, so a bug here cannot change anything."""

    def __init__(self, context, runner):
        self.context = context
        self.runner = runner

    def _kubectl(self, *args):
        try:
            done = self.runner(["kubectl", f"--context={self.context}", *args], capture_output=True, text=True,
                               errors="replace", timeout=COMMAND_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return Output(None, "", "")
        return Output(done.returncode, done.stdout, done.stderr)

    def get(self, namespace, *args):
        output = self._kubectl("-n", namespace, "get", *args, "-o", "json")
        if output.code != 0:
            raise Unread(read_failure(output, "kubernetes_read_failed"))
        return json.loads(output.stdout)

    def exec(self, pod, *command):
        if not any(allowed.fullmatch(" ".join(command)) for allowed in READ_ONLY_POD_COMMANDS):
            raise ValueError("refusing a command outside the read-only list")
        return self._kubectl("-n", NAMESPACE, "exec", pod, "-c", CONTAINER, "--", *command)


def read_failure(output, default):
    if output.code is None:
        return "kubectl_unavailable"
    return "kubernetes_forbidden" if "forbidden" in output.stderr.lower() else default


def route_contracts():
    """The README's three allowed routes, taken from the renderer so the two cannot drift apart."""
    routes = []
    for side in ("target", "source"):
        peer_side = "source" if side == "target" else "target"
        peer = network.ingress(peer_side)["metadata"]["annotations"]
        # egress() only checks the shape of this name; nothing ever contacts it.
        fqdn = f"{peer['tailscale.com/hostname']}.example.ts.net"
        for item in network.egress(side, fqdn):
            if item["kind"] != "ProxyGroup":
                continue
            group = item["metadata"]["name"]
            own_db = network.ingress(side)["metadata"]["annotations"]
            routes.append({
                "route": f"{group.removeprefix('bravo-')}-to-{peer_side}-db", "side": side, "proxy_group": group,
                "from_tag": item["spec"]["tags"][0], "peer": peer["tailscale.com/hostname"],
                "peer_tag": peer["tailscale.com/tags"],
                "own_db": own_db["tailscale.com/hostname"], "own_db_tag": own_db["tailscale.com/tags"],
            })
    return routes


def find_pod(kube, parent, parent_type, parent_namespace=None):
    labels = {"tailscale.com/managed": "true", "tailscale.com/parent-resource": parent,
              "tailscale.com/parent-resource-type": parent_type}
    if parent_namespace:
        labels["tailscale.com/parent-resource-ns"] = parent_namespace
    pods = kube.get(NAMESPACE, "pods", "-l", ",".join(f"{key}={value}" for key, value in labels.items()))["items"]
    running = [pod for pod in pods
               if pod["status"].get("phase") == "Running" and "deletionTimestamp" not in pod["metadata"]]
    if len(running) != 1:
        raise Unread("proxy_pod_not_unique")
    return running[0]["metadata"]["name"]


def status_path(peer):
    """The order `tailscale status` uses. Relay names the peer's home DERP even on a direct path."""
    if not peer.get("Active"):
        return None
    if peer.get("CurAddr"):
        return DIRECT
    if peer.get("PeerRelay"):
        return PEER_RELAY
    return DERP if peer.get("Relay") else None


def addresses(peer):
    found = {"ipv4": None, "ipv6": None}
    for text in peer["TailscaleIPs"]:
        address = ipaddress.ip_address(text)
        found[f"ipv{address.version}"] = found[f"ipv{address.version}"] or str(address)
    return found


def read_status(kube, pod):
    """Reduce `tailscale status --json` to what the checks use; account fields stop here."""
    output = kube.exec(pod, "tailscale", SOCKET, "status", "--json")
    if output.code != 0:
        raise Unread(read_failure(output, "status_read_failed"))
    try:
        status = json.loads(output.stdout)
        if status["BackendState"] != "Running":
            raise Unread("tailscale_not_running")
        peers = [{"name": peer["DNSName"].split(".")[0], **addresses(peer), "tags": peer.get("Tags") or [],
                  "online": peer.get("Online") is True, "path": status_path(peer)}
                 for peer in (status.get("Peer") or {}).values()]
        return {"self_tags": status["Self"].get("Tags") or [], "peers": peers}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Unread("status_unparseable") from None


def named(peers, name):
    return next((peer for peer in peers if peer["name"] == name), None)


def parse_pongs(text):
    pongs = []
    for via, amount, unit in PONG.findall(text):
        ms = float(amount) * (1000 if unit == "s" else 1)
        derp = PONG_DERP.fullmatch(via)
        if derp:
            pongs.append(Pong(DERP, derp.group(1), ms))
        elif PONG_PEER_RELAY.fullmatch(via):
            pongs.append(Pong(PEER_RELAY, None, ms))
        elif PONG_DIRECT.fullmatch(via):
            pongs.append(Pong(DIRECT, None, ms))
    return pongs


def tailscale_ping(kube, pod, address):
    return parse_pongs(kube.exec(pod, "tailscale", SOCKET, "ping", f"--c={PONG_COUNT}", "--timeout=3s",
                                 "--until-direct=false", address).stdout)


def measure_path(kube, pod, peer_name, address):
    pongs = tailscale_ping(kube, pod, address)
    # Read after pinging: an idle peer reports no current path.
    current = named(read_status(kube, pod)["peers"], peer_name) or {}
    last = pongs[-1].kind if pongs else None
    samples = [pong.ms for pong in pongs if pong.kind == last]
    kind, reason = None, None
    if not pongs:
        reason = "no_pong"
    elif current.get("path") is None:
        reason = "path_status_unread"
    elif current["path"] != last:
        reason = "path_status_disagrees_with_ping"
    else:
        kind = last
    return {
        "kind": kind, "derp_region": pongs[-1].region if kind == DERP else None,
        "peer_online": current.get("online"),
        "pongs": {name: sum(pong.kind == name for pong in pongs) for name in (DIRECT, DERP, PEER_RELAY)},
        "disco_rtt_ms": {"samples": len(samples), "min": min(samples), "avg": round(sum(samples) / len(samples), 1),
                         "max": max(samples)} if samples else None,
        # Only the reading is judged: nothing in the design or repository sets a value for it.
        "verdict": PASS if kind else UNKNOWN, "reason": reason,
    }


def df_ping(kube, pod, flag, payload, address):
    """(answered, average rtt): answered is None when the output is not a ping summary at all."""
    output = kube.exec(pod, "ping", flag, "-M", "do", "-c", "3", "-i", "0.2", "-W", "2", "-s", str(payload), address)
    summary = PING_SUMMARY.search(output.stdout)
    if summary is None:
        return None, None
    rtt = PING_RTT.search(output.stdout)
    return int(summary.group(1)) > 0, float(rtt.group(1)) if rtt else None


def largest_answered(send, floor, ceiling):
    """Largest size `send` answers, given that floor was answered. None if a probe becomes unreadable."""
    full_size = ceiling
    answered = send(full_size)
    if answered:
        return full_size
    while answered is not None and ceiling - floor > 1:
        middle = (floor + ceiling) // 2
        answered = send(middle)
        if answered:
            floor = middle
        else:
            ceiling = middle
    if answered is None:
        return None
    # Asked again now that the search has taken a while: bursts lost while the path settled are not a black hole.
    again = send(full_size)
    if again:
        return full_size
    # The floor must still answer. If it does not, the path went away and this says nothing about size.
    return floor if again is False and send(floor) else None


def largest_df_payload(kube, pod, flag, address, ceiling):
    return largest_answered(lambda size: df_ping(kube, pod, flag, size, address)[0], BASELINE_PAYLOAD, ceiling)


def measure_mtu(kube, pod, peer):
    def unknown(reason, interface=None):
        return {"interface_bytes": interface, "df_max_packet_bytes": None, "df_family": None, "icmp_rtt_ms": None,
                "verdict": UNKNOWN, "reason": reason}

    read = kube.exec(pod, "cat", MTU_FILE)
    text = read.stdout.strip()
    if read.code != 0 or not text.isdigit() or int(text) < MIN_INTERFACE_MTU:
        return unknown("interface_mtu_unread")
    interface = int(text)
    # An ingress proxy forwards every protocol on its IPv4 tailnet address to the Service's ClusterIP
    # (containerboot installIngressForwardingRule), and whether that answers ICMP depends on kube-proxy.
    # Its IPv6 address is not forwarded when the ClusterIP is IPv4, so the proxy itself should answer there.
    reason = "df_probe_no_reply"
    for family, flag, header in ICMP_FAMILIES:
        if peer[family] is None:
            continue
        answered, rtt = df_ping(kube, pod, flag, BASELINE_PAYLOAD, peer[family])
        if answered is None:
            reason = "df_probe_unread"
        if not answered:
            continue
        payload = largest_df_payload(kube, pod, flag, peer[family], interface - header)
        if payload is None:
            return unknown("df_probe_unread", interface)
        largest = payload + header
        # The comparison needs no outside number: a size the interface offers but the path drops stalls TCP.
        below = largest < interface
        return {"interface_bytes": interface, "df_max_packet_bytes": largest, "df_family": family, "icmp_rtt_ms": rtt,
                "verdict": FAIL if below else PASS, "reason": "df_below_interface_mtu" if below else None}
    return unknown(reason, interface)


def tcp_probe(kube, pod, address, port):
    if address is None:
        return UNREAD
    output = kube.exec(pod, "nc", "-vv", "-z", "-w", "3", address, str(port))
    if output.code == 0 and NC_OPEN.search(output.stderr):
        return CONNECTED
    if output.code == 1 and NC_REFUSED.search(output.stderr):
        return REFUSED
    if output.code == 1 and NC_TIMEOUT.search(output.stderr):
        return TIMEOUT
    return UNREAD


def judge_allow(observed):
    if observed == CONNECTED:
        return {"verdict": PASS, "reason": None}
    if observed == UNREAD:
        return {"verdict": UNKNOWN, "reason": "allow_probe_unread"}
    return {"verdict": FAIL, "reason": "allow_not_connected"}


def judge_deny(observed, source_alive, destination_alive):
    """A denial counts only if the same run shows this Pod can connect and the target can be reached."""
    if observed == CONNECTED:
        return {"verdict": FAIL, "reason": "deny_connected"}
    if observed == UNREAD:
        return {"verdict": UNKNOWN, "reason": "deny_probe_unread"}
    # tailscale sends no RST for a denied SYN (net/tstun/wrap.go), so a refusal came from past the ACL.
    if observed == REFUSED:
        return {"verdict": UNKNOWN, "reason": "deny_refused_acl_unproven"}
    if source_alive and destination_alive:
        return {"verdict": PASS, "reason": None}
    return {"verdict": UNKNOWN, "reason": "deny_control_missing"}


def judge_wrong_port(control, observed):
    """Two ports on the peer's IPv6 address, which its proxy does not forward to the Service.

    Nothing listens on either there. A port the ACL allows is refused by the proxy's kernel; a port it
    blocks stays silent because tailscale sends no RST. The allowed port shows the comparison works.
    The peerapi port cannot serve here: tailscale lets it through the filter whatever the ACL says.
    """
    if observed in (CONNECTED, REFUSED):
        return {"verdict": FAIL, "reason": "wrong_port_open"}
    if UNREAD in (control, observed):
        return {"verdict": UNKNOWN, "reason": "wrong_port_probe_unread"}
    if control == TIMEOUT:
        return {"verdict": UNKNOWN, "reason": "wrong_port_control_missing"}
    return {"verdict": PASS, "reason": None}


def own_db_listening(kube, side):
    """A database that is down is unreachable too, which would make its denial pass by accident."""
    service = network.ingress(side)["metadata"]
    try:
        pod = find_pod(kube, service["name"], "svc", service["namespace"])
        tagged = service["annotations"]["tailscale.com/tags"] in read_status(kube, pod)["self_tags"]
        address = ipaddress.ip_address(kube.get(service["namespace"], "service", service["name"])["spec"]["clusterIP"])
        return tagged and tcp_probe(kube, pod, str(address), ALLOWED_PORT) == CONNECTED
    except (Unread, ValueError, KeyError, TypeError):
        return False


def probe_forbidden(kube, pod, peers, route):
    """(worst outcome, tunnels up) over every visible peer with the forbidden tag or name.

    The tag matters: a re-registered node gets a suffixed name and keeps the same access.
    A peer missing from this node's list has no tunnel. A TCP attempt would follow the Pod's default
    route instead, possibly out through the node's own tailscale identity, and measure something else.
    """
    forbidden = [peer for peer in peers if route["own_db_tag"] in peer["tags"] or peer["name"] == route["own_db"]]
    if not forbidden:
        return NO_PEER, True
    observed = {tcp_probe(kube, pod, peer["ipv4"], ALLOWED_PORT) for peer in forbidden}
    worst = next(outcome for outcome in (CONNECTED, UNREAD, REFUSED, TIMEOUT) if outcome in observed)
    # A dead tunnel times out too. Discovery pings do not pass through the packet filter.
    tunnels_up = worst != TIMEOUT or all(tailscale_ping(kube, pod, peer["ipv4"]) for peer in forbidden)
    return worst, tunnels_up


def measure_acl(kube, pod, route, peers, own_db_alive):
    peer = named(peers, route["peer"])
    allow_observed = tcp_probe(kube, pod, peer["ipv4"], ALLOWED_PORT)
    source_alive = allow_observed == CONNECTED
    control = tcp_probe(kube, pod, peer["ipv6"], ALLOWED_PORT)
    wrong_port = tcp_probe(kube, pod, peer["ipv6"], WRONG_PORT)
    observed, tunnels_up = probe_forbidden(kube, pod, peers, route)
    destination_alive = own_db_alive and tunnels_up
    return {"allow": {"target": f"{route['peer']}:{ALLOWED_PORT}", "observed": allow_observed,
                      **judge_allow(allow_observed)},
            "deny": [{"probe": "wrong_port", "target": f"{route['peer']}:{WRONG_PORT}", "observed": wrong_port,
                      "control_observed": control, **judge_wrong_port(control, wrong_port)},
                     {"probe": "forbidden_peer", "target": f"{route['own_db']}:{ALLOWED_PORT}", "observed": observed,
                      "source_alive": source_alive, "destination_alive": destination_alive,
                      **judge_deny(observed, source_alive, destination_alive)}]}


def combine(verdicts):
    verdicts = list(verdicts)
    if not verdicts:
        return UNKNOWN
    if FAIL in verdicts:
        return FAIL
    return UNKNOWN if UNKNOWN in verdicts else PASS


def stopped(route, verdict, reason):
    return {"route": route["route"], "side": route["side"], "from_tag": route["from_tag"], "to_tag": route["peer_tag"],
            "verdict": verdict, "reasons": [reason], "path": None, "mtu": None, "acl": None}


def strip_reason(item):
    return {key: value for key, value in item.items() if key != "reason"}


def measure_route(kube, route, own_db_alive):
    pod = find_pod(kube, route["proxy_group"], "proxygroup")
    first = read_status(kube, pod)
    if route["from_tag"] not in first["self_tags"]:
        return stopped(route, FAIL, "self_tag_mismatch")
    peer = named(first["peers"], route["peer"])
    if peer is None:
        return stopped(route, FAIL, "peer_not_visible")
    if route["peer_tag"] not in peer["tags"]:
        return stopped(route, FAIL, "peer_tag_mismatch")
    if peer["ipv4"] is None:
        raise Unread("peer_address_unread")
    path = measure_path(kube, pod, route["peer"], peer["ipv4"])
    mtu = measure_mtu(kube, pod, peer)
    acl = measure_acl(kube, pod, route, first["peers"], own_db_alive)
    parts = [path, mtu, acl["allow"], *acl["deny"]]
    return {"route": route["route"], "side": route["side"], "from_tag": route["from_tag"], "to_tag": route["peer_tag"],
            "verdict": combine(part["verdict"] for part in parts),
            "reasons": list(dict.fromkeys(part["reason"] for part in parts if part["reason"])),
            "path": strip_reason(path), "mtu": strip_reason(mtu),
            "acl": {"allow": strip_reason(acl["allow"]), "deny": [strip_reason(entry) for entry in acl["deny"]]}}


def check(contexts, runner):
    results, own_db = [], {}
    for route in route_contracts():
        side = route["side"]
        if not contexts.get(side):
            results.append(stopped(route, UNKNOWN, "context_not_provided"))
            continue
        kube = Kube(contexts[side], runner)
        try:
            if side not in own_db:
                own_db[side] = own_db_listening(kube, side)
            results.append(measure_route(kube, route, own_db[side]))
        except Unread as error:
            results.append(stopped(route, UNKNOWN, str(error)))
        except Exception as error:  # noqa: BLE001 - Only the type is reported, so no value can leak.
            results.append(stopped(route, UNKNOWN, f"unexpected_{type(error).__name__}"))
    return {
        "check": "tailnet-path", "schema_version": 1,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "overall": combine(result["verdict"] for result in results), "routes": results,
        "not_judged": {"fields": ["path.kind", "path.derp_region", "path.disco_rtt_ms", "path.peer_online",
                                  "mtu.icmp_rtt_ms", "mtu.interface_bytes"],
                       "why": "no threshold in the design document or repository; reported as values only"},
        "not_measured": [
            ("a rule opening some other single port on the peer, or written for its IPv4 address only: "
            "only port 22 on its IPv6 address is compared with the allowed port"),
            "the hops from a workload to its egress proxy and from the ingress proxy to the database, including MTU",
        ],
        "scope": "one snapshot per route; discovery and ICMP round trips, not database latency, p95 or an SLO",
        "raw_output_suppressed": True, "policy_changed": False, "node_settings_changed": False,
    }


def main(argv=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-context", help="kubectl context of the source cluster")
    parser.add_argument("--target-context", help="kubectl context of the target cluster")
    args = parser.parse_args(argv)
    if not (args.source_context or args.target_context):
        parser.error("give --source-context, --target-context or both")
    report = check({"source": args.source_context, "target": args.target_context}, runner)
    print(json.dumps(report, indent=2))
    return EXIT_CODES[report["overall"]]


if __name__ == "__main__":
    sys.exit(main())
