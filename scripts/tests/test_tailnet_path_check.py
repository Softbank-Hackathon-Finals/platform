"""Tailnet path check: direct or relayed, MTU, ACL allow/deny, and UNKNOWN whenever it cannot read.

The status JSON follows ipnstate.Status at tailscale v1.104.4, the version pinned in network.py
(ipn/ipnstate/ipnstate.go, cmd/tailscale/cli/status.go, cmd/tailscale/cli/ping.go). `nc` wording was
captured from Alpine's BusyBox and `ping` wording comes from iputils 20240905, which Alpine 3.22 ships
for the proxy image. Every identifier below is made up; nothing was read from a real tailnet.
"""
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = ROOT / "infra/envs/target-workloads"
sys.path.insert(0, str(WORKLOADS))
check = importlib.import_module("tailnet_path_check")

REPLICATION = "target-replication-to-source-db"
POOLER = "target-pooler-to-source-db"
REVERSE = "source-replication-to-target-db"

TAILNET = "fixture-tailnet"
LOGIN = "person@example.invalid"
UNDERLAY = "203.0.113.10:41641"
RELAY_ADDRESS = "198.51.100.7:7777:vni:1"
NODES = {
    "bravo-source-db": ("100.64.0.1", "fd7a:115c:a1e0::1", "tag:bravo-source-db"),
    "bravo-target-db": ("100.64.0.2", "fd7a:115c:a1e0::2", "tag:bravo-target-db"),
    "bravo-target-db-1": ("100.64.0.6", "fd7a:115c:a1e0::6", "tag:bravo-target-db"),
    "bravo-target-replication-0": ("100.64.0.3", "fd7a:115c:a1e0::3", "tag:bravo-target-replication"),
    "bravo-target-pooler-0": ("100.64.0.4", "fd7a:115c:a1e0::4", "tag:bravo-target-pooler"),
    "bravo-source-replication-0": ("100.64.0.5", "fd7a:115c:a1e0::5", "tag:bravo-source-replication"),
}
# label value on the Pod -> (cluster, parent type, Pod name, the tailnet node that Pod is)
PODS = {
    "bravo-target-replication": ("target", "proxygroup", "bravo-target-replication-0", "bravo-target-replication-0"),
    "bravo-target-pooler": ("target", "proxygroup", "bravo-target-pooler-0", "bravo-target-pooler-0"),
    "bravo-source-replication": ("source", "proxygroup", "bravo-source-replication-0", "bravo-source-replication-0"),
    "source-db-tailnet": ("source", "svc", "ts-source-db-tailnet-fixture-0", "bravo-source-db"),
    "target-db-tailnet": ("target", "svc", "ts-target-db-tailnet-fixture-0", "bravo-target-db"),
}
# Correct ACL: each node's list holds only the peers its grants connect it to.
NETMAP = {
    "bravo-target-replication-0": ["bravo-source-db"],
    "bravo-target-pooler-0": ["bravo-source-db"],
    "bravo-source-replication-0": ["bravo-target-db"],
    "bravo-source-db": ["bravo-target-replication-0", "bravo-target-pooler-0"],
    "bravo-target-db": ["bravo-source-replication-0"],
}
CLUSTER_IPS = {"source-db-tailnet": "10.96.0.11", "target-db-tailnet": "10.96.0.12"}
SERVICE_NAMESPACES = {"source-db-tailnet": "source-dev", "target-db-tailnet": "target-dev"}
# A correct ACL hides the forbidden database, so these tests make it visible to reach the TCP attempt.
FORBIDDEN_VISIBLE = {"bravo-target-replication-0": ["bravo-source-db", "bravo-target-db"]}
FORBIDDEN_NC = "bravo-target-replication-0 -c tailscale -- nc -vv -z -w 3 100.64.0.2 5432"
OPEN, REFUSES, SILENT = "open", "refused", "timeout"
IPV6 = "ipv6"  # Marks a canned TCP answer as being for the peer's IPv6 address.


def peer_status(node, **fields):
    """One ipnstate.PeerStatus with every non-omitempty key the struct has at v1.104.4."""
    ipv4, ipv6, tag = NODES[node]
    return {
        "ID": f"stable-{node}", "NodeID": 7654321, "PublicKey": f"nodekey:{node}", "HostName": node,
        "DNSName": f"{node}.{TAILNET}.ts.net.", "OS": "linux", "UserID": 1234567,
        "TailscaleIPs": [ipv4, ipv6], "AllowedIPs": [f"{ipv4}/32", f"{ipv6}/128"], "Tags": [tag],
        "Addrs": [UNDERLAY], "CurAddr": UNDERLAY, "Relay": "tok", "PeerRelay": "",
        "RxBytes": 1024, "TxBytes": 2048, "Created": "2026-11-01T00:00:00Z",
        "LastWrite": "2026-11-06T00:00:00Z", "LastSeen": "2026-11-06T00:00:00Z",
        "LastHandshake": "2026-11-06T00:00:00Z", "Online": True, "ExitNode": False, "ExitNodeOption": False,
        "Active": True, "PeerAPIURL": [f"http://{ipv4}:12345", f"http://[{ipv6}]:12345"], "TaildropTarget": 0, "NoFileSharingReason": "",
        "InNetworkMap": True, "InMagicSock": True, "InEngine": True, **fields,
    }


def status_json(node, peers, peer_fields, backend="Running", self_tags=None):
    """One ipnstate.Status, including the account fields the tool must never repeat."""
    own = peer_status(node)
    if self_tags is not None:
        own["Tags"] = self_tags
    return json.dumps({
        "Version": "1.104.4-fixture", "TUN": True, "BackendState": backend, "AuthURL": "",
        "TailscaleIPs": NODES[node][:2], "Self": own, "Health": [], "MagicDNSSuffix": f"{TAILNET}.ts.net",
        "CurrentTailnet": {"Name": f"{TAILNET}.example", "StableID": "stable-tailnet",
                           "MagicDNSSuffix": f"{TAILNET}.ts.net", "MagicDNSEnabled": True},
        "CertDomains": [f"{node}.{TAILNET}.ts.net"], "ExtraRecords": [],
        "Peer": {f"nodekey:{peer}": peer_status(peer, **peer_fields.get(peer, peer_fields.get("*", {})))
                 for peer in peers},
        "User": {"1234567": {"ID": 1234567, "LoginName": LOGIN, "DisplayName": "Fixture Person",
                             "ProfilePicURL": "https://example.invalid/p.png"}},
        "ClientVersion": None,
    })


def done(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def nc_output(address, port, outcome):
    # BusyBox prints an IPv6 target with brackets inside the parentheses.
    target = f"{address} ([{address}]:{port})" if ":" in address else f"{address} ({address}:{port})"
    if outcome == OPEN:
        return done(0, "", f"{target} open\nsent 0, rcvd 0\n")
    text = {REFUSES: "Connection refused", SILENT: "Operation timed out"}[outcome]
    return done(1, "", f"nc: {target}: {text}\nsent 0, rcvd 0\ncommand terminated with exit code 1\n")


def df_ping_output(address, payload, replied):
    if not replied:
        return done(1, f"PING {address} ({address}) {payload} data bytes\n\n--- {address} ping statistics ---\n"
                       "3 packets transmitted, 0 received, 100% packet loss, time 2051ms\n\n",
                    "command terminated with exit code 1\n")
    return done(0, f"PING {address} ({address}) {payload} data bytes\n"
                   f"{payload + 8} bytes from {address}: icmp_seq=1 ttl=64 time=31.2 ms\n\n"
                   f"--- {address} ping statistics ---\n"
                   "3 packets transmitted, 3 received, 0% packet loss, time 402ms\n"
                   "rtt min/avg/max/mdev = 30.100/31.200/33.000/1.000 ms\n")


class FakeCluster:
    """Stands in for kubectl: reads the same argv the tool sends and answers like the real pieces."""

    def __init__(self, peer_fields=None, pong_via=UNDERLAY, interface_mtu="1280\n", path_limit=1280,
                 ipv4_answers=False, ipv6_answers=True, netmap=None, tcp=None, intercept=None, backend="Running",
                 self_tags=None, pods=None):
        self.peer_fields = peer_fields or {}
        self.pong_via = pong_via
        self.interface_mtu = interface_mtu
        self.path_limit = path_limit
        self.ipv4_answers = ipv4_answers
        self.ipv6_answers = ipv6_answers
        self.netmap = {**NETMAP, **(netmap or {})}
        self.tcp = tcp or {}
        self.intercept = intercept
        self.backend = backend
        self.self_tags = self_tags or {}
        self.pods = pods or {}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs["timeout"] == check.COMMAND_TIMEOUT_S
        if self.intercept:
            answer = self.intercept(" ".join(argv))
            if answer is not None:
                return answer
        side = "source" if "source" in argv[1] else "target"
        if argv[4] == "get":
            return self._get(side, argv)
        assert argv[2:4] == ["-n", "tailscale-system"]
        assert argv[6:8] == ["-c", "tailscale"]
        pod = argv[5]
        node = next(entry[3] for entry in PODS.values() if entry[2] == pod)
        return self._exec(node, argv[argv.index("--") + 1:])

    def _get(self, side, argv):
        assert argv[-2:] == ["-o", "json"]
        if argv[5] == "service":
            assert argv[2:4] == ["-n", SERVICE_NAMESPACES[argv[6]]]
            return done(0, json.dumps({"spec": {"clusterIP": CLUSTER_IPS[argv[6]]}}))
        assert argv[2:4] == ["-n", "tailscale-system"]
        labels = dict(item.split("=") for item in argv[7].split(","))
        parent = labels["tailscale.com/parent-resource"]
        cluster, kind, pod, _ = PODS.get(parent, (None, None, None, None))
        assert labels["tailscale.com/managed"] == "true"
        if kind == "svc":
            assert labels["tailscale.com/parent-resource-ns"] == SERVICE_NAMESPACES[parent]
        matched = cluster == side and labels["tailscale.com/parent-resource-type"] == kind
        items = [{"metadata": {"name": pod}, "status": {"phase": "Running"}}] if matched else []
        return done(0, json.dumps({"items": self.pods.get(parent, items)}))

    def _exec(self, node, command):
        joined = " ".join(command)
        if command[0] == "cat":
            return done(0, self.interface_mtu)
        if command[0] == "nc":
            return self._tcp(node, command[-2], int(command[-1]))
        if command[0] == "ping":
            return self._df_ping(command)
        if "status --json" in joined:
            return done(0, status_json(node, self.netmap.get(node, []), self.peer_fields, self.backend,
                                       self.self_tags.get(node)))
        if " ping " in joined:
            address = command[-1]
            return done(0, "".join(f"pong from peer ({address}) via {self.pong_via} in 12ms\n" for _ in range(3)))
        raise AssertionError(f"unexpected command: {joined}")

    def _tcp(self, node, address, port):
        peer = next((name for name, values in NODES.items() if address in values[:2]), address)
        ipv6 = ":" in address
        key = (node, peer, port, IPV6) if ipv6 else (node, peer, port)
        if key in self.tcp:
            return nc_output(address, port, self.tcp[key])
        allowed = peer in NETMAP.get(node, []) and port == 5432
        if ipv6:  # The proxy does not forward this address and nothing listens: the ACL decides the answer.
            return nc_output(address, port, REFUSES if allowed else SILENT)
        return nc_output(address, port, OPEN if allowed or address in CLUSTER_IPS.values() else SILENT)

    def _df_ping(self, command):
        version, payload, address = command[1], int(command[command.index("-s") + 1]), command[-1]
        header, answers = (28, self.ipv4_answers) if version == "-4" else (48, self.ipv6_answers)
        return df_ping_output(address, payload, answers and payload + header <= self.path_limit)

    def pod_commands(self, pod=None):
        return [" ".join(call[call.index("--") + 1:]) for call in self.calls
                if "--" in call and (pod is None or call[5] == pod)]


def fail_on(fragment, *result):
    """Answer one command with a canned failure; everything else behaves normally."""
    return lambda command: done(*result) if fragment in command else None


def run(cluster=None, source="source-ctx", target="target-ctx"):
    return check.check({"source": source, "target": target}, cluster or FakeCluster())


def route(report, name):
    return next(item for item in report["routes"] if item["route"] == name)


def forbidden_peer(result):
    return next(item for item in result["acl"]["deny"] if item["probe"] == "forbidden_peer")


def wrong_port(result):
    return next(item for item in result["acl"]["deny"] if item["probe"] == "wrong_port")


class HealthyTailnetTests(unittest.TestCase):
    def test_correct_tailnet_passes_every_route(self):
        report = run()
        self.assertEqual(report["overall"], "PASS")
        self.assertEqual({item["route"]: item["verdict"] for item in report["routes"]},
                         {REPLICATION: "PASS", POOLER: "PASS", REVERSE: "PASS"})
        self.assertEqual(route(report, REPLICATION)["reasons"], [])

    def test_routes_are_the_readme_acl_table(self):
        readme = (WORKLOADS / "README.md").read_text(encoding="utf-8")
        rows = set(re.findall(r"^\| `(bravo-[a-z-]+)` \| `(bravo-[a-z]+-db)` \| `chat_[a-z]+` \|$", readme, re.MULTILINE))
        self.assertEqual(len(rows), 3)
        self.assertEqual({(item["proxy_group"], item["peer"]) for item in check.route_contracts()}, rows)


class PathKindTests(unittest.TestCase):
    def test_current_address_means_direct_even_though_relay_is_also_filled(self):
        # Relay always names the peer's home DERP; only an empty CurAddr means traffic uses it.
        cluster = FakeCluster(peer_fields={"*": {"CurAddr": UNDERLAY, "Relay": "tok"}})
        path = route(run(cluster), REPLICATION)["path"]
        self.assertEqual(path["kind"], "direct")
        self.assertIsNone(path["derp_region"])
        self.assertEqual(path["pongs"], {"direct": 3, "derp": 0, "peer_relay": 0})

    def test_relayed_path_reports_the_region_and_is_not_a_failure(self):
        cluster = FakeCluster(peer_fields={"*": {"CurAddr": ""}}, pong_via="DERP(tok)")
        result = route(run(cluster), REPLICATION)
        self.assertEqual(result["path"]["kind"], "derp")
        self.assertEqual(result["path"]["derp_region"], "tok")
        self.assertEqual(result["path"]["pongs"], {"direct": 0, "derp": 3, "peer_relay": 0})
        self.assertEqual(result["verdict"], "PASS")

    def test_peer_relay_is_its_own_kind(self):
        cluster = FakeCluster(peer_fields={"*": {"CurAddr": "", "PeerRelay": RELAY_ADDRESS}},
                              pong_via=f"peer-relay({RELAY_ADDRESS})")
        path = route(run(cluster), REPLICATION)["path"]
        self.assertEqual(path["kind"], "peer_relay")
        self.assertIsNone(path["derp_region"])
        self.assertEqual(path["pongs"]["peer_relay"], 3)

    def test_round_trip_is_reported_from_the_pongs(self):
        path = route(run(), REPLICATION)["path"]
        self.assertEqual(path["disco_rtt_ms"], {"samples": 3, "min": 12.0, "avg": 12.0, "max": 12.0})

    def test_seconds_in_a_pong_are_converted(self):
        def slow(command):
            if " ping " in command:
                return done(0, f"pong from peer (100.64.0.1) via {UNDERLAY} in 1.5s\n")
            return None
        self.assertEqual(route(run(FakeCluster(intercept=slow)), REPLICATION)["path"]["disco_rtt_ms"]["max"], 1500.0)

    def test_online_state_is_reported_as_read(self):
        self.assertIs(route(run(), REPLICATION)["path"]["peer_online"], True)
        offline = FakeCluster(peer_fields={"bravo-source-db": {"Online": False}})
        self.assertIs(route(run(offline), REPLICATION)["path"]["peer_online"], False)

    def test_region_is_not_reported_unless_the_path_is_relayed(self):
        path = route(run(FakeCluster(pong_via="DERP(tok)")), REPLICATION)["path"]
        self.assertIsNone(path["kind"])
        self.assertIsNone(path["derp_region"])

    def test_idle_peer_is_not_assumed_to_be_relayed(self):
        # An idle peer has Relay filled and CurAddr empty, which also looks like DERP.
        cluster = FakeCluster(peer_fields={"*": {"CurAddr": "", "Active": False}}, pong_via="DERP(tok)")
        result = route(run(cluster), REPLICATION)
        self.assertIsNone(result["path"]["kind"])
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("path_status_unread", result["reasons"])

    def test_status_and_ping_disagreeing_is_unknown(self):
        cluster = FakeCluster(peer_fields={"*": {"CurAddr": ""}}, pong_via=UNDERLAY)
        result = route(run(cluster), REPLICATION)
        self.assertIsNone(result["path"]["kind"])
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("path_status_disagrees_with_ping", result["reasons"])

    def test_path_upgrading_mid_ping_reports_the_final_path_and_its_timing(self):
        def upgrading(command):
            if " ping " in command:
                return done(0, "pong from peer (100.64.0.1) via DERP(tok) in 90ms\n"
                               f"pong from peer (100.64.0.1) via {UNDERLAY} in 10ms\n")
            return None
        path = route(run(FakeCluster(intercept=upgrading)), REPLICATION)["path"]
        self.assertEqual(path["kind"], "direct")
        self.assertEqual(path["pongs"], {"direct": 1, "derp": 1, "peer_relay": 0})
        self.assertEqual(path["disco_rtt_ms"], {"samples": 1, "min": 10.0, "avg": 10.0, "max": 10.0})

    def test_no_pong_is_unknown(self):
        cluster = FakeCluster(intercept=fail_on(" ping ", 1, 'ping "100.64.0.1" timed out\n', "no reply\n"))
        result = route(run(cluster), REPLICATION)
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("no_pong", result["reasons"])

    def test_unrecognized_path_in_a_pong_is_not_counted(self):
        cluster = FakeCluster(peer_fields={"*": {"CurAddr": ""}}, pong_via="DERP(tok;rm)")
        result = route(run(cluster), REPLICATION)
        self.assertIn("no_pong", result["reasons"])
        self.assertEqual(result["verdict"], "UNKNOWN")


class ReadFailureTests(unittest.TestCase):
    def assert_unknown(self, cluster, reason, name=REPLICATION):
        report = run(cluster)
        result = route(report, name)
        self.assertEqual(result["verdict"], "UNKNOWN", result)
        self.assertIn(reason, result["reasons"])
        self.assertNotEqual(report["overall"], "PASS")

    def test_status_that_is_not_json(self):
        self.assert_unknown(FakeCluster(intercept=fail_on("status --json", 0, "not json")), "status_unparseable")

    def test_status_with_the_wrong_shape(self):
        for body in ("[]", '{"BackendState": "Running"}', '{"BackendState": "Running", "Self": null}'):
            with self.subTest(body=body):
                self.assert_unknown(FakeCluster(intercept=fail_on("status --json", 0, body)), "status_unparseable")

    def test_status_command_failing_is_not_trusted_even_with_output(self):
        valid = status_json("bravo-target-replication-0", ["bravo-source-db"], {})
        self.assert_unknown(FakeCluster(intercept=fail_on("status --json", 1, valid, "failed\n")), "status_read_failed")

    def test_tailscale_not_running(self):
        self.assert_unknown(FakeCluster(backend="NeedsLogin"), "tailscale_not_running")

    def test_kubectl_missing(self):
        def missing(_argv, **_):
            raise FileNotFoundError("kubectl")
        report = check.check({"source": "source-ctx", "target": "target-ctx"}, missing)
        self.assertEqual(report["overall"], "UNKNOWN")
        self.assertIn("kubectl_unavailable", route(report, REPLICATION)["reasons"])

    def test_kubectl_hanging(self):
        def hanging(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        report = check.check({"source": "source-ctx", "target": "target-ctx"}, hanging)
        self.assertEqual(report["overall"], "UNKNOWN")

    def test_forbidden_is_named_so_the_operator_knows_it_is_their_access(self):
        self.assert_unknown(FakeCluster(intercept=fail_on(" get pods ", 1, "", 'Error from server (Forbidden): pods is forbidden\n')),
                            "kubernetes_forbidden")

    def test_no_proxy_pod(self):
        self.assert_unknown(FakeCluster(pods={"bravo-target-replication": []}), "proxy_pod_not_unique")

    def test_two_proxy_pods(self):
        pod = {"metadata": {"name": "bravo-target-replication-0"}, "status": {"phase": "Running"}}
        self.assert_unknown(FakeCluster(pods={"bravo-target-replication": [pod, pod]}), "proxy_pod_not_unique")

    def test_pod_that_is_not_running(self):
        pod = {"metadata": {"name": "bravo-target-replication-0"}, "status": {"phase": "Pending"}}
        self.assert_unknown(FakeCluster(pods={"bravo-target-replication": [pod]}), "proxy_pod_not_unique")

    def test_pod_being_deleted_is_not_used(self):
        pod = {"metadata": {"name": "bravo-target-replication-0", "deletionTimestamp": "2026-11-06T00:00:00Z"},
               "status": {"phase": "Running"}}
        self.assert_unknown(FakeCluster(pods={"bravo-target-replication": [pod]}), "proxy_pod_not_unique")

    def test_peer_without_a_usable_address(self):
        self.assert_unknown(FakeCluster(peer_fields={"*": {"TailscaleIPs": ["not-an-address"]}}), "status_unparseable")

    def test_peer_with_no_ipv4_address_cannot_be_probed(self):
        self.assert_unknown(FakeCluster(peer_fields={"*": {"TailscaleIPs": ["fd7a:115c:a1e0::1"]}}), "peer_address_unread")

    def test_unexpected_error_becomes_unknown_with_only_its_type(self):
        def broken(command):
            if "status --json" in command:
                raise RuntimeError("secret detail 100.64.0.9")
        report = run(FakeCluster(intercept=broken))
        self.assertEqual(route(report, REPLICATION)["verdict"], "UNKNOWN")
        self.assertIn("unexpected_RuntimeError", route(report, REPLICATION)["reasons"])
        self.assertNotIn("secret detail", json.dumps(report))


class CommandFailureTests(unittest.TestCase):
    def test_ping_without_df_support_is_unknown_not_a_small_mtu(self):
        # BusyBox's ping rejects -M; the Tailscale image should carry iputils, but that is unmeasured.
        usage = "ping: unrecognized option: M\nBusyBox v1.37.0 multi-call binary.\n\nUsage: ping [OPTIONS] HOST\n"
        result = route(run(FakeCluster(intercept=fail_on(" -M do ", 1, "", usage))), REPLICATION)
        self.assertEqual(result["mtu"]["verdict"], "UNKNOWN")
        self.assertIsNone(result["mtu"]["df_max_packet_bytes"])
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("df_probe_unread", result["reasons"])

    def test_interface_mtu_unreadable(self):
        for result_args in ((1, "", "cat: can't open\n"), (1, "1280\n", ""), (0, "lots\n"), (0, "99\n")):
            with self.subTest(args=result_args):
                result = route(run(FakeCluster(intercept=fail_on("/sys/class/net", *result_args))), REPLICATION)
                self.assertEqual(result["mtu"]["verdict"], "UNKNOWN")
                self.assertIn("interface_mtu_unread", result["reasons"])

    def test_nc_usage_error_on_a_forbidden_port_is_not_a_denial(self):
        # Exit code 1 and no connection: exactly what a naive check counts as "denied".
        usage = "nc: unrecognized option: z\nBusyBox v1.37.0 multi-call binary.\n"
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, intercept=fail_on(FORBIDDEN_NC, 1, "", usage))
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "unread")
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("deny_probe_unread", result["reasons"])

    def test_kubernetes_api_refusal_is_not_mistaken_for_the_probe_being_refused(self):
        api_error = "Error from server: error dialing backend: dial tcp 10.0.0.1:10250: connect: connection refused\n"
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, intercept=fail_on(FORBIDDEN_NC, 1, "", api_error))
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "unread")
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_kubernetes_timeout_is_not_mistaken_for_the_probe_timing_out(self):
        api_error = "Unable to connect to the server: net/http: TLS handshake timed out\n"
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, intercept=fail_on(FORBIDDEN_NC, 1, "", api_error))
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "unread")
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")

    def test_probe_text_must_come_with_the_matching_exit_code(self):
        for code, text in ((0, "nc: x (x:5432): Connection refused\n"), (0, "nc: x (x:5432): Operation timed out\n"),
                           (1, "x (x:5432) open\n")):
            with self.subTest(code=code, text=text):
                cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, intercept=fail_on(FORBIDDEN_NC, code, "", text))
                self.assertEqual(forbidden_peer(route(run(cluster), REPLICATION))["observed"], "unread")

    def test_nc_usage_error_on_the_allowed_port_is_unknown(self):
        def usage_on_allow(command):
            if command.endswith("100.64.0.1 5432"):
                return done(1, "", "nc: unrecognized option: z\n")
            return None
        result = route(run(FakeCluster(intercept=usage_on_allow)), REPLICATION)
        self.assertEqual(result["acl"]["allow"]["verdict"], "UNKNOWN")
        self.assertIn("allow_probe_unread", result["reasons"])
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_network_unreachable_is_not_a_timeout(self):
        unreachable = "nc: 100.64.0.2 (100.64.0.2:5432): Network unreachable\ncommand terminated with exit code 1\n"
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, intercept=fail_on(FORBIDDEN_NC, 1, "", unreachable))
        self.assertEqual(forbidden_peer(route(run(cluster), REPLICATION))["verdict"], "UNKNOWN")


class AclTests(unittest.TestCase):
    def test_probes_cover_the_allowed_port_a_wrong_port_and_the_forbidden_database(self):
        result = route(run(), REPLICATION)
        self.assertEqual(result["acl"]["allow"], {"target": "bravo-source-db:5432", "observed": "connected", "verdict": "PASS"})
        self.assertEqual(wrong_port(result), {"probe": "wrong_port", "target": "bravo-source-db:22", "observed": "timeout",
                                              "control_observed": "refused", "verdict": "PASS"})
        self.assertEqual(forbidden_peer(result)["target"], "bravo-target-db:5432")
        self.assertEqual(forbidden_peer(result)["observed"], "no_tailnet_peer")
        self.assertEqual(forbidden_peer(result)["verdict"], "PASS")

    def test_allow_timing_out_fails(self):
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 5432): SILENT})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(result["acl"]["allow"]["verdict"], "FAIL")
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("allow_not_connected", result["reasons"])

    def test_allow_refused_fails(self):
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 5432): REFUSES})
        self.assertEqual(route(run(cluster), REPLICATION)["verdict"], "FAIL")

    def test_wrong_port_connecting_fails(self):
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 22, IPV6): OPEN})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(wrong_port(result)["verdict"], "FAIL")
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("wrong_port_open", result["reasons"])

    def test_rule_opening_every_port_fails_because_the_wrong_port_is_refused(self):
        # A blocked SYN gets no RST, so a refusal here means the packet passed the ACL.
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 22, IPV6): REFUSES})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(wrong_port(result)["observed"], "refused")
        self.assertEqual(wrong_port(result)["verdict"], "FAIL")
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("wrong_port_open", result["reasons"])

    def test_wrong_port_silence_needs_the_allowed_port_answering_on_the_same_address(self):
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 5432, IPV6): SILENT})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(wrong_port(result)["control_observed"], "timeout")
        self.assertEqual(wrong_port(result)["verdict"], "UNKNOWN")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("wrong_port_control_missing", result["reasons"])

    def test_something_listening_on_the_allowed_port_is_also_a_working_control(self):
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 5432, IPV6): OPEN})
        self.assertEqual(wrong_port(route(run(cluster), REPLICATION))["verdict"], "PASS")

    def test_refusal_fails_even_when_the_control_cannot_be_read(self):
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 22, IPV6): REFUSES},
                              intercept=fail_on("fd7a:115c:a1e0::1 5432", 1, "", "nc: unrecognized option: z\n"))
        result = route(run(cluster), REPLICATION)
        self.assertEqual(wrong_port(result)["control_observed"], "unread")
        self.assertEqual(wrong_port(result)["verdict"], "FAIL")

    def test_wrong_port_probe_that_cannot_be_read_is_unknown(self):
        breaks = {"wrong port": " 22", "control": "fd7a:115c:a1e0::1 5432"}
        for name, fragment in breaks.items():
            with self.subTest(name):
                cluster = FakeCluster(intercept=fail_on(fragment, 1, "", "nc: unrecognized option: z\n"))
                result = route(run(cluster), REPLICATION)
                self.assertEqual(wrong_port(result)["verdict"], "UNKNOWN")
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertIn("wrong_port_probe_unread", result["reasons"])

    def test_kubernetes_errors_on_the_wrong_port_probe_are_unknown(self):
        for text in ("Error from server: error dialing backend: connect: connection refused\n",
                     "Unable to connect to the server: net/http: TLS handshake timed out\n"):
            with self.subTest(text=text):
                result = route(run(FakeCluster(intercept=fail_on(" 22", 1, "", text))), REPLICATION)
                self.assertEqual(wrong_port(result)["observed"], "unread")
                self.assertEqual(result["verdict"], "UNKNOWN")

    def test_peer_without_ipv6_cannot_prove_the_port_limit(self):
        cluster = FakeCluster(ipv4_answers=True, peer_fields={"bravo-source-db": {"TailscaleIPs": ["100.64.0.1"]}})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(result["mtu"]["verdict"], "PASS")
        self.assertEqual(wrong_port(result)["verdict"], "UNKNOWN")
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_wrong_port_probes_use_the_address_the_proxy_does_not_forward(self):
        cluster = FakeCluster()
        run(cluster)
        commands = cluster.pod_commands("bravo-target-replication-0")
        self.assertIn("nc -vv -z -w 3 fd7a:115c:a1e0::1 22", commands)
        self.assertIn("nc -vv -z -w 3 fd7a:115c:a1e0::1 5432", commands)
        self.assertNotIn("nc -vv -z -w 3 100.64.0.1 22", commands)

    def test_broad_rule_reaching_the_forbidden_database_fails(self):
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE,
                              tcp={("bravo-target-replication-0", "bravo-target-db", 5432): OPEN})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "connected")
        self.assertEqual(forbidden_peer(result)["verdict"], "FAIL")
        self.assertEqual(result["verdict"], "FAIL")

    def test_allow_and_deny_swapped_fails_every_probe(self):
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 5432): SILENT,
                                   ("bravo-target-replication-0", "bravo-source-db", 22, IPV6): REFUSES})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(result["acl"]["allow"]["verdict"], "FAIL")
        self.assertEqual(wrong_port(result)["verdict"], "FAIL")
        self.assertEqual(result["verdict"], "FAIL")

    def test_denial_does_not_count_when_the_allowed_route_is_also_down(self):
        # A dead tunnel times out everywhere. Without a working control that says nothing about the ACL.
        cluster = FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 5432): SILENT})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")
        self.assertIn("deny_control_missing", result["reasons"])

    def test_refusal_on_a_forbidden_port_is_not_proof_of_the_acl(self):
        # tailscale v1.104.4 sends no RST for a denied SYN (net/tstun/wrap.go), so a refusal got past the ACL.
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, tcp={("bravo-target-replication-0", "bravo-target-db", 5432): REFUSES})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "refused")
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("deny_refused_acl_unproven", result["reasons"])

    def test_forbidden_database_visible_but_not_connecting_passes_only_after_a_real_attempt(self):
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE)
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "timeout")
        self.assertEqual(forbidden_peer(result)["verdict"], "PASS")
        self.assertIn("nc -vv -z -w 3 100.64.0.2 5432", cluster.pod_commands("bravo-target-replication-0"))

    def test_absent_peer_gets_no_tcp_attempt_because_it_would_leave_the_tailnet(self):
        cluster = FakeCluster()
        run(cluster)
        self.assertNotIn("nc -vv -z -w 3 100.64.0.2 5432", cluster.pod_commands("bravo-target-replication-0"))

    def test_forbidden_database_under_a_suffixed_name_is_found_by_its_tag(self):
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE,
                              peer_fields={"bravo-target-db": {"DNSName": f"bravo-target-db-1.{TAILNET}.ts.net."}},
                              tcp={("bravo-target-replication-0", "bravo-target-db", 5432): OPEN})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "connected")
        self.assertEqual(result["verdict"], "FAIL")

    def test_stale_copy_of_the_forbidden_database_does_not_hide_the_live_one(self):
        cluster = FakeCluster(netmap={"bravo-target-replication-0": ["bravo-source-db", "bravo-target-db", "bravo-target-db-1"]},
                              tcp={("bravo-target-replication-0", "bravo-target-db-1", 5432): OPEN})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "connected")
        self.assertEqual(result["verdict"], "FAIL")

    def test_forbidden_name_with_a_different_tag_is_still_probed(self):
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, peer_fields={"bravo-target-db": {"Tags": ["tag:other"]}},
                              tcp={("bravo-target-replication-0", "bravo-target-db", 5432): OPEN})
        self.assertEqual(forbidden_peer(route(run(cluster), REPLICATION))["observed"], "connected")

    def test_peer_sharing_a_name_does_not_hide_the_tagged_forbidden_database(self):
        # A node shared in from another tailnet can carry the same first label.
        cluster = FakeCluster(netmap={"bravo-target-replication-0": ["bravo-source-db", "bravo-target-db", "bravo-target-db-1"]},
                              peer_fields={"bravo-target-db-1": {"DNSName": "bravo-target-db.shared.ts.net.", "Tags": []}},
                              tcp={("bravo-target-replication-0", "bravo-target-db", 5432): OPEN})
        self.assertEqual(forbidden_peer(route(run(cluster), REPLICATION))["observed"], "connected")

    def test_visible_forbidden_peer_with_no_tunnel_proves_nothing(self):
        # A dead tunnel times out as well. Discovery pings do not pass through the packet filter.
        no_tunnel = "bravo-target-replication-0 -c tailscale -- tailscale --socket=/tmp/tailscaled.sock ping --c=3 --timeout=3s --until-direct=false 100.64.0.2"
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, intercept=fail_on(no_tunnel, 1, "", "no reply\n"))
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "timeout")
        self.assertFalse(forbidden_peer(result)["destination_alive"])
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")
        self.assertIn("deny_control_missing", result["reasons"])

    def test_every_visible_forbidden_peer_needs_a_tunnel(self):
        no_tunnel = "bravo-target-replication-0 -c tailscale -- tailscale --socket=/tmp/tailscaled.sock ping --c=3 --timeout=3s --until-direct=false 100.64.0.6"
        cluster = FakeCluster(netmap={"bravo-target-replication-0": ["bravo-source-db", "bravo-target-db", "bravo-target-db-1"]},
                              intercept=fail_on(no_tunnel, 1, "", "no reply\n"))
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")
        self.assertIn("deny_control_missing", result["reasons"])

    def test_visible_forbidden_peer_with_no_ipv4_cannot_be_probed(self):
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE, peer_fields={"bravo-target-db": {"TailscaleIPs": ["fd7a:115c:a1e0::2"]}})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(forbidden_peer(result)["observed"], "unread")
        self.assertIn("deny_probe_unread", result["reasons"])

    def test_forbidden_database_that_is_not_listening_proves_nothing(self):
        def closed(command):
            if "ts-target-db-tailnet-fixture-0" in command and " nc " in command:
                return nc_output("10.96.0.12", 5432, REFUSES)
            return None
        result = route(run(FakeCluster(intercept=closed)), REPLICATION)
        self.assertFalse(forbidden_peer(result)["destination_alive"])
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_forbidden_database_with_no_proxy_pod_proves_nothing(self):
        result = route(run(FakeCluster(pods={"target-db-tailnet": []})), REPLICATION)
        self.assertFalse(forbidden_peer(result)["destination_alive"])
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")

    def test_forbidden_database_proxy_with_the_wrong_tag_proves_nothing(self):
        result = route(run(FakeCluster(self_tags={"bravo-target-db": ["tag:other"]})), REPLICATION)
        self.assertFalse(forbidden_peer(result)["destination_alive"])
        self.assertEqual(forbidden_peer(result)["verdict"], "UNKNOWN")

    def test_one_side_run_needs_only_that_sides_cluster(self):
        report = run(source=None)
        self.assertEqual(route(report, REPLICATION)["verdict"], "PASS")
        self.assertEqual(route(report, POOLER)["verdict"], "PASS")

    def test_proxy_with_the_wrong_tag_fails_before_any_probe(self):
        cluster = FakeCluster(self_tags={"bravo-target-replication-0": ["tag:other"]})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["reasons"], ["self_tag_mismatch"])
        self.assertIsNone(result["acl"])

    def test_peer_with_the_wrong_tag_fails_before_any_probe(self):
        cluster = FakeCluster(peer_fields={"bravo-source-db": {"Tags": ["tag:other"]}})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["reasons"], ["peer_tag_mismatch"])

    def test_peer_without_tags_is_a_mismatch(self):
        def untagged(command):
            if "status --json" in command and "bravo-target-replication-0" in command:
                status = json.loads(status_json("bravo-target-replication-0", ["bravo-source-db"], {}))
                next(iter(status["Peer"].values())).pop("Tags")
                return done(0, json.dumps(status))
            return None
        self.assertEqual(route(run(FakeCluster(intercept=untagged)), REPLICATION)["reasons"], ["peer_tag_mismatch"])

    def test_allowed_peer_missing_from_the_node_list_fails(self):
        cluster = FakeCluster(netmap={"bravo-target-replication-0": []})
        result = route(run(cluster), REPLICATION)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["reasons"], ["peer_not_visible"])

    def test_name_match_is_exact_so_a_renamed_duplicate_does_not_count(self):
        def renamed(command):
            if "status --json" in command and "bravo-target-replication-0" in command:
                status = json.loads(status_json("bravo-target-replication-0", ["bravo-source-db"], {}))
                next(iter(status["Peer"].values()))["DNSName"] = f"bravo-source-db-1.{TAILNET}.ts.net."
                return done(0, json.dumps(status))
            return None
        self.assertEqual(route(run(FakeCluster(intercept=renamed)), REPLICATION)["reasons"], ["peer_not_visible"])


class MtuTests(unittest.TestCase):
    def test_full_size_packets_pass_over_ipv6_when_ipv4_is_forwarded_to_the_service(self):
        # An ingress proxy forwards its whole IPv4 tailnet address to the Service, so ICMP there gets no answer.
        mtu = route(run(), REPLICATION)["mtu"]
        self.assertEqual(mtu, {"interface_bytes": 1280, "df_max_packet_bytes": 1280, "df_family": "ipv6",
                               "icmp_rtt_ms": 31.2, "verdict": "PASS"})

    def test_ipv4_is_used_when_it_answers(self):
        cluster = FakeCluster(ipv4_answers=True)
        mtu = route(run(cluster), REPLICATION)["mtu"]
        self.assertEqual((mtu["df_family"], mtu["df_max_packet_bytes"]), ("ipv4", 1280))
        self.assertIn("ping -4 -M do -c 3 -i 0.2 -W 2 -s 1252 100.64.0.1", cluster.pod_commands())

    def test_ipv4_black_hole_counts_the_ipv4_header(self):
        result = route(run(FakeCluster(ipv4_answers=True, path_limit=1270)), REPLICATION)
        self.assertEqual(result["mtu"]["df_max_packet_bytes"], 1270)
        self.assertEqual(result["mtu"]["verdict"], "FAIL")

    def test_probes_set_the_df_bit_and_never_exceed_the_interface(self):
        cluster = FakeCluster()
        run(cluster)
        probes = [command for command in cluster.pod_commands() if command.startswith("ping ")]
        self.assertTrue(probes)
        self.assertTrue(all(" -M do " in command for command in probes))
        self.assertIn("ping -6 -M do -c 3 -i 0.2 -W 2 -s 1232 fd7a:115c:a1e0::1", probes)
        ipv6_sizes = [int(re.search(r"-s (\d+)", command).group(1)) for command in probes if " -6 " in command]
        self.assertEqual(max(ipv6_sizes), 1232)

    def test_black_hole_below_the_interface_mtu_fails_with_the_exact_size(self):
        result = route(run(FakeCluster(path_limit=1200)), REPLICATION)
        self.assertEqual(result["mtu"]["df_max_packet_bytes"], 1200)
        self.assertEqual(result["mtu"]["verdict"], "FAIL")
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("df_below_interface_mtu", result["reasons"])

    def test_one_lost_burst_at_full_size_is_not_a_black_hole(self):
        seen = []

        def lost_once(command):
            if " -s 1232 " in command and not seen:
                seen.append(command)
                return done(1, df_ping_output("x", 1232, False).stdout)
            return None
        mtu = route(run(FakeCluster(intercept=lost_once)), REPLICATION)["mtu"]
        self.assertEqual((mtu["df_max_packet_bytes"], mtu["verdict"]), (1280, "PASS"))

    def test_several_lost_bursts_while_the_path_settles_are_not_a_black_hole(self):
        seen = []

        def lost_at_first(command):
            if " -M do " in command and " -s 56 " not in command and len(seen) < 3:
                seen.append(command)
                return done(1, df_ping_output("x", 1, False).stdout)
            return None
        mtu = route(run(FakeCluster(intercept=lost_at_first)), REPLICATION)["mtu"]
        self.assertEqual((mtu["df_max_packet_bytes"], mtu["verdict"]), (1280, "PASS"))

    def test_full_size_probe_unreadable_at_the_second_ask_is_unknown(self):
        asked = []

        def second_breaks(command):
            if " -s 1232 " in command:
                asked.append(command)
                return done(1, df_ping_output("x", 1232, False).stdout) if len(asked) == 1 else done(1, "", "error\n")
            return None
        self.assertEqual(route(run(FakeCluster(intercept=second_breaks)), REPLICATION)["mtu"]["verdict"], "UNKNOWN")

    def test_path_going_away_during_the_search_is_unknown_not_a_small_mtu(self):
        baselines = []

        def goes_away(command):
            if "bravo-target-replication-0" not in command or " -M do " not in command or " -6 " not in command:
                return None
            if " -s 56 " in command:
                baselines.append(command)
                if len(baselines) == 1:
                    return None
            return done(1, df_ping_output("x", 1, False).stdout)
        result = route(run(FakeCluster(intercept=goes_away)), REPLICATION)
        self.assertEqual(len(baselines), 2)
        self.assertIsNone(result["mtu"]["df_max_packet_bytes"])
        self.assertEqual(result["mtu"]["verdict"], "UNKNOWN")

    def test_interface_mtu_value_is_reported_without_being_judged(self):
        result = route(run(FakeCluster(interface_mtu="1400\n", path_limit=1400)), REPLICATION)
        self.assertEqual(result["mtu"]["interface_bytes"], 1400)
        self.assertEqual(result["mtu"]["verdict"], "PASS")

    def test_no_reply_on_either_family_is_unknown_not_a_small_mtu(self):
        result = route(run(FakeCluster(ipv4_answers=False, ipv6_answers=False)), REPLICATION)
        self.assertIsNone(result["mtu"]["df_max_packet_bytes"])
        self.assertEqual(result["mtu"]["verdict"], "UNKNOWN")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("df_probe_no_reply", result["reasons"])

    def test_probe_breaking_mid_search_is_unknown(self):
        def breaking(command):
            if " -s 1232 " in command:
                return done(0, df_ping_output("x", 1232, False).stdout)
            if " -s 644 " in command:
                return done(1, "", "error: unable to upgrade connection\n")
            return None
        result = route(run(FakeCluster(intercept=breaking)), REPLICATION)
        self.assertIsNone(result["mtu"]["df_max_packet_bytes"])
        self.assertEqual(result["mtu"]["verdict"], "UNKNOWN")

    def test_one_reply_out_of_three_counts_as_passing(self):
        def lossy(command):
            if " -s 1232 " in command:
                return done(0, "3 packets transmitted, 1 received, 66% packet loss, time 402ms\n"
                               "rtt min/avg/max/mdev = 31.000/31.000/31.000/0.000 ms\n")
            return None
        self.assertEqual(route(run(FakeCluster(intercept=lossy)), REPLICATION)["mtu"]["df_max_packet_bytes"], 1280)

    def test_busybox_style_summary_is_not_accepted_as_a_df_result(self):
        def plain(command):
            if " -M do " in command:
                return done(0, "3 packets transmitted, 3 packets received, 0% packet loss\n")
            return None
        self.assertEqual(route(run(FakeCluster(intercept=plain)), REPLICATION)["mtu"]["verdict"], "UNKNOWN")


class ReportTests(unittest.TestCase):
    def test_exit_codes_tell_the_three_outcomes_apart(self):
        self.assertEqual(check.EXIT_CODES, {"PASS": 0, "FAIL": 1, "UNKNOWN": 2})

    def test_fail_outranks_unknown_and_unknown_outranks_pass(self):
        self.assertEqual(check.combine(["PASS", "UNKNOWN", "FAIL"]), "FAIL")
        self.assertEqual(check.combine(["PASS", "UNKNOWN"]), "UNKNOWN")
        self.assertEqual(check.combine(["PASS", "PASS"]), "PASS")

    def test_nothing_measured_is_not_a_pass(self):
        self.assertEqual(check.combine([]), "UNKNOWN")

    def test_side_without_a_context_stays_unknown_and_is_never_contacted(self):
        cluster = FakeCluster()
        report = run(cluster, source=None)
        self.assertEqual(route(report, REVERSE)["verdict"], "UNKNOWN")
        self.assertEqual(route(report, REVERSE)["reasons"], ["context_not_provided"])
        self.assertEqual(report["overall"], "UNKNOWN")
        self.assertFalse([call for call in cluster.calls if "source" in call[1]])

    def test_one_failing_route_fails_the_whole_report(self):
        cluster = FakeCluster(tcp={("bravo-target-pooler-0", "bravo-source-db", 5432): SILENT})
        report = run(cluster)
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(route(report, REVERSE)["verdict"], "PASS")

    def test_report_is_bound_to_the_exact_tool_bytes(self):
        expected = hashlib.sha256((WORKLOADS / "tailnet_path_check.py").read_bytes()).hexdigest()
        self.assertEqual(run()["source_sha256"], expected)

    def test_unjudged_values_are_named_in_the_report(self):
        self.assertEqual(set(run()["not_judged"]["fields"]),
                         {"path.kind", "path.derp_region", "path.disco_rtt_ms", "path.peer_online", "mtu.icmp_rtt_ms",
                          "mtu.interface_bytes"})

    def test_report_names_what_a_pass_does_not_cover(self):
        report = run()
        self.assertEqual(len(report["not_measured"]), 2)
        self.assertTrue(any("other single port" in item and "IPv4 address only" in item for item in report["not_measured"]))

    def test_main_prints_json_and_returns_the_exit_code(self):
        for cluster, overall, expected in (
                (FakeCluster(), "PASS", 0),
                (FakeCluster(tcp={("bravo-target-replication-0", "bravo-source-db", 5432): SILENT}), "FAIL", 1),
                (FakeCluster(backend="Stopped"), "UNKNOWN", 2)):
            with self.subTest(expected=expected):
                with tempfile.TemporaryFile("w+") as stdout:
                    saved, sys.stdout = sys.stdout, stdout
                    try:
                        code = check.main(["--source-context", "source-ctx", "--target-context", "target-ctx"], cluster)
                    finally:
                        sys.stdout = saved
                    stdout.seek(0)
                    self.assertEqual(json.load(stdout)["overall"], overall)
                self.assertEqual(code, expected)

    def test_running_without_any_context_is_refused(self):
        with self.assertRaises(SystemExit) as raised, open(os.devnull, "w") as sink:
            saved, sys.stderr = sys.stderr, sink
            try:
                check.main([], FakeCluster())
            finally:
                sys.stderr = saved
        self.assertEqual(raised.exception.code, 2)

    def test_real_script_without_kubectl_reports_unknown_on_stdout(self):
        with tempfile.TemporaryDirectory() as empty:
            done_run = subprocess.run(
                [sys.executable, str(WORKLOADS / "tailnet_path_check.py"), "--target-context", "target-ctx"],
                env={"PATH": empty}, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done_run.returncode, 2)
        self.assertEqual(json.loads(done_run.stdout)["overall"], "UNKNOWN")


class PrivacyTests(unittest.TestCase):
    SECRETS = (LOGIN, "1234567", "7654321", TAILNET, "nodekey:", "stable-", UNDERLAY.split(":", maxsplit=1)[0], "198.51.100.7",
               "100.64.", "fd7a:", "10.96.", "ts.net", "example", "fixture-context")

    def assert_clean(self, cluster):
        report = run(cluster, source="fixture-context-source", target="fixture-context-target")
        text = json.dumps(report)
        for secret in self.SECRETS:
            self.assertNotIn(secret, text)
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text))
        return report

    def test_healthy_report_holds_no_identifier(self):
        self.assertEqual(self.assert_clean(FakeCluster())["overall"], "PASS")

    def test_relayed_report_holds_no_identifier(self):
        cluster = FakeCluster(peer_fields={"*": {"CurAddr": ""}}, pong_via="DERP(tok)")
        self.assertEqual(route(self.assert_clean(cluster), REPLICATION)["path"]["derp_region"], "tok")

    def test_peer_relay_report_holds_no_identifier(self):
        report = self.assert_clean(FakeCluster(peer_fields={"*": {"CurAddr": "", "PeerRelay": RELAY_ADDRESS}},
                                               pong_via=f"peer-relay({RELAY_ADDRESS})"))
        self.assertEqual(route(report, REPLICATION)["path"]["kind"], "peer_relay")

    def test_error_text_from_commands_is_not_copied(self):
        noisy = "failed for person@example.invalid at 100.64.0.1 in fixture-tailnet.ts.net\n"
        def noisy_reads(command):
            return done(1, noisy, noisy) if " exec " in command and "status --json" not in command else None
        report = self.assert_clean(FakeCluster(intercept=noisy_reads))
        self.assertIsNotNone(route(report, REPLICATION)["path"])

    def test_failure_reports_hold_no_identifier(self):
        report = self.assert_clean(FakeCluster(netmap=FORBIDDEN_VISIBLE,
                                               tcp={("bravo-target-replication-0", "bravo-target-db", 5432): OPEN}))
        self.assertEqual(report["overall"], "FAIL")


class ReadOnlyTests(unittest.TestCase):
    def test_a_full_run_uses_only_get_and_exec(self):
        cluster = FakeCluster()
        run(cluster)
        self.assertEqual({call[4] for call in cluster.calls}, {"get", "exec"})

    def test_every_pod_command_is_a_known_read(self):
        cluster = FakeCluster(netmap=FORBIDDEN_VISIBLE)
        run(cluster)
        shapes = {re.sub(r"\S+$", "ADDR", re.sub(r" \d+$", "", command)) for command in cluster.pod_commands()}
        shapes = {re.sub(r"-s \d+", "-s N", shape) for shape in shapes}
        self.assertEqual(shapes, {
            "tailscale --socket=/tmp/tailscaled.sock status ADDR",
            "tailscale --socket=/tmp/tailscaled.sock ping --c=3 --timeout=3s --until-direct=false ADDR",
            "cat ADDR",
            "ping -4 -M do -c 3 -i 0.2 -W 2 -s N ADDR",
            "ping -6 -M do -c 3 -i 0.2 -W 2 -s N ADDR",
            "nc -vv -z -w 3 ADDR",
        })

    def test_commands_outside_the_list_are_refused_before_kubectl_runs(self):
        cluster = FakeCluster()
        kube = check.Kube("target-ctx", cluster)
        for command in (("tailscale", "up"), ("tailscale", "logout"), ("tailscale", "set", "--shields-up"),
                        ("sh", "-c", "tailscale status"), ("nc", "-e", "/bin/sh", "100.64.0.1", "5432"),
                        ("nc", "-vv", "-z", "-w", "3", "100.64.0.1;rm", "5432"),
                        ("cat", "/etc/passwd"), ("ping", "-c", "1", "100.64.0.1"),
                        ("tailscale", "--socket=/tmp/tailscaled.sock", "status", "--json", "--peers=false", "extra")):
            with self.subTest(command=command), self.assertRaises(ValueError):
                kube.exec("pod", *command)
        self.assertEqual(cluster.calls, [])

    def test_tcp_probe_sends_no_data_flag_and_no_stdin(self):
        cluster = FakeCluster()
        run(cluster)
        probes = [command for command in cluster.pod_commands() if command.startswith("nc ")]
        self.assertTrue(probes)
        self.assertTrue(all(" -z " in command for command in probes))
        for call in (call for call in cluster.calls if call[4] == "exec"):
            flags = call[:call.index("--")]
            self.assertNotIn("-i", flags)
            self.assertNotIn("-t", flags)

    def test_context_cannot_be_read_as_a_kubectl_flag(self):
        calls = []

        def recording(argv, **_):
            calls.append(argv)
            return done(0, "1280\n")
        check.Kube("--kubeconfig=/tmp/x", recording).exec("pod", "cat", "/sys/class/net/tailscale0/mtu")
        self.assertEqual(calls[0][1], "--context=--kubeconfig=/tmp/x")


if __name__ == "__main__":
    unittest.main()
