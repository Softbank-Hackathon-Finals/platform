"""Remove each safety rule from a temporary copy of the tool and require its tests to fail.

Passing tests only help if they would notice a rule going missing. The repository files are never
edited. A mutation that no longer matches the source fails here too, so it cannot silently stop
measuring.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = "infra/envs/target-workloads"
TOOL = "tailnet_path_check.py"
# Only a test method counts. An import error or a crash in setup measures nothing.
RESULT_LINE = re.compile(r"^(?:FAIL|ERROR): (test_\w+) \(test_tailnet_path_check\.", re.MULTILINE)

MUTATIONS = [
    # Direct or relayed.
    ("relay_checked_before_current_address",
     ('    if peer.get("CurAddr"):\n        return DIRECT\n    if peer.get("PeerRelay"):\n'
     '        return PEER_RELAY\n    return DERP if peer.get("Relay") else None'),
     ('    if peer.get("Relay"):\n        return DERP\n    if peer.get("CurAddr"):\n        return DIRECT\n'
     '    return PEER_RELAY if peer.get("PeerRelay") else None')),
    ("idle_peer_counts_as_current", '    if not peer.get("Active"):\n        return None\n', ""),
    ("ping_disagreement_ignored", '    elif current["path"] != last:\n        reason = "path_status_disagrees_with_ping"\n', ""),
    ("no_pong_still_reads_status", '    if not pongs:\n        reason = "no_pong"\n    elif current', "    if current"),
    ("region_validation", r'PONG_DERP = re.compile(r"DERP\(([a-z0-9-]{1,32})\)")', r'PONG_DERP = re.compile(r"DERP\((.*)\)")'),
    ("timing_mixes_old_paths", "samples = [pong.ms for pong in pongs if pong.kind == last]", "samples = [pong.ms for pong in pongs]"),
    ("derp_fails_the_route", '"verdict": PASS if kind else UNKNOWN, "reason": reason,',
     '"verdict": PASS if kind == DIRECT else UNKNOWN, "reason": reason,'),
    # Unreadable values stay UNKNOWN.
    ("status_exit_code_ignored",
     '    if output.code != 0:\n        raise Unread(read_failure(output, "status_read_failed"))\n', ""),
    ("backend_state_ignored",
     '        if status["BackendState"] != "Running":\n            raise Unread("tailscale_not_running")\n', ""),
    ("unknown_counts_as_pass", "    return UNKNOWN if UNKNOWN in verdicts else PASS", "    return PASS"),
    ("nothing_measured_passes", "    if not verdicts:\n        return UNKNOWN\n", ""),
    ("fail_does_not_outrank_unknown", '    if FAIL in verdicts:\n        return FAIL\n    return UNKNOWN if UNKNOWN in verdicts else PASS',
     '    if UNKNOWN in verdicts:\n        return UNKNOWN\n    return FAIL if FAIL in verdicts else PASS'),
    ("unknown_exit_code_is_success", "EXIT_CODES = {PASS: 0, FAIL: 1, UNKNOWN: 2}", "EXIT_CODES = {PASS: 0, FAIL: 1, UNKNOWN: 0}"),
    ("missing_context_passes", 'stopped(route, UNKNOWN, "context_not_provided")', 'stopped(route, PASS, "context_not_provided")'),
    ("several_pods_accepted", "    if len(running) != 1:\n", "    if not running:\n"),
    ("pending_pod_accepted", 'if pod["status"].get("phase") == "Running" and "deletionTimestamp"', 'if "deletionTimestamp"'),
    ("deleting_pod_accepted", ' and "deletionTimestamp" not in pod["metadata"]', ""),
    ("interface_mtu_sanity", ' or int(text) < MIN_INTERFACE_MTU', ""),
    # Probe text must come from nc itself, and only with the matching exit code.
    ("nc_refused_matches_any_text", r'NC_REFUSED = re.compile(r"^nc: \S+ \(\S+\): Connection refused$", re.MULTILINE)',
     'NC_REFUSED = re.compile(r"connection refused", re.I)'),
    ("nc_timeout_matches_any_text", r'NC_TIMEOUT = re.compile(r"^nc: \S+ \(\S+\): (?:Connection|Operation) timed out$", re.MULTILINE)',
     'NC_TIMEOUT = re.compile(r"timed out", re.I)'),
    ("nc_failure_means_timeout", "    return UNREAD\n\n\ndef judge_allow", "    return TIMEOUT\n\n\ndef judge_allow"),
    ("nc_exit_code_ignored_for_refused", "if output.code == 1 and NC_REFUSED", "if NC_REFUSED"),
    ("nc_exit_code_ignored_for_timeout", "if output.code == 1 and NC_TIMEOUT", "if NC_TIMEOUT"),
    ("nc_exit_code_ignored_for_open", "if output.code == 0 and NC_OPEN", "if NC_OPEN"),
    # Allow and deny.
    ("allow_failure_is_only_unknown", 'return {"verdict": FAIL, "reason": "allow_not_connected"}',
     'return {"verdict": UNKNOWN, "reason": "allow_not_connected"}'),
    ("allow_unread_counts_as_failure", 'return {"verdict": UNKNOWN, "reason": "allow_probe_unread"}',
     'return {"verdict": FAIL, "reason": "allow_probe_unread"}'),
    ("deny_connecting_passes", 'return {"verdict": FAIL, "reason": "deny_connected"}', 'return {"verdict": PASS, "reason": None}'),
    ("wrong_port_refusal_counts_as_denied", "if observed in (CONNECTED, REFUSED):", "if observed == CONNECTED:"),
    ("wrong_port_connection_ignored", "if observed in (CONNECTED, REFUSED):", "if observed == REFUSED:"),
    ("wrong_port_unread_passes", 'return {"verdict": UNKNOWN, "reason": "wrong_port_probe_unread"}', 'return {"verdict": PASS, "reason": None}'),
    ("wrong_port_control_unread_ignored", "if UNREAD in (control, observed):", "if observed == UNREAD:"),
    ("wrong_port_control_ignored", '    if control == TIMEOUT:\n        return {"verdict": UNKNOWN, "reason": "wrong_port_control_missing"}\n', ""),
    ("wrong_port_uses_forwarded_address", 'wrong_port = tcp_probe(kube, pod, peer["ipv6"], WRONG_PORT)', 'wrong_port = tcp_probe(kube, pod, peer["ipv4"], WRONG_PORT)'),
    ("control_uses_forwarded_address", 'control = tcp_probe(kube, pod, peer["ipv6"], ALLOWED_PORT)', 'control = tcp_probe(kube, pod, peer["ipv4"], ALLOWED_PORT)'),
    ("forbidden_found_only_by_name", 'if route["own_db_tag"] in peer["tags"] or peer["name"] == route["own_db"]]', 'if peer["name"] == route["own_db"]]'),
    ("forbidden_found_only_by_tag", 'if route["own_db_tag"] in peer["tags"] or peer["name"] == route["own_db"]]', 'if route["own_db_tag"] in peer["tags"]]'),
    ("peers_keyed_by_name", "        return {\"self_tags\": status[\"Self\"].get(\"Tags\") or [], \"peers\": peers}",
     "        return {\"self_tags\": status[\"Self\"].get(\"Tags\") or [], \"peers\": list({p[\"name\"]: p for p in peers}.values())}"),
    ("worst_outcome_not_first", "(CONNECTED, UNREAD, REFUSED, TIMEOUT) if outcome", "(TIMEOUT, REFUSED, UNREAD, CONNECTED) if outcome"),
    ("tunnel_control_skipped", 'tunnels_up = worst != TIMEOUT or all(tailscale_ping(kube, pod, peer["ipv4"]) for peer in forbidden)',
     "tunnels_up = True"),
    ("one_tunnel_is_enough", 'all(tailscale_ping(kube, pod, peer["ipv4"]) for peer in forbidden)', 'any(tailscale_ping(kube, pod, peer["ipv4"]) for peer in forbidden)'),
    ("unread_control_hides_refusal", """    if observed in (CONNECTED, REFUSED):
        return {"verdict": FAIL, "reason": "wrong_port_open"}
    if UNREAD in (control, observed):
        return {"verdict": UNKNOWN, "reason": "wrong_port_probe_unread"}
""", """    if UNREAD in (control, observed):
        return {"verdict": UNKNOWN, "reason": "wrong_port_probe_unread"}
    if observed in (CONNECTED, REFUSED):
        return {"verdict": FAIL, "reason": "wrong_port_open"}
"""),
    ("tunnel_control_ignored", "destination_alive = own_db_alive and tunnels_up", "destination_alive = own_db_alive"),
    ("missing_address_crashes", "    if address is None:\n        return UNREAD\n", ""),
    ("deny_unread_passes", 'return {"verdict": UNKNOWN, "reason": "deny_probe_unread"}', 'return {"verdict": PASS, "reason": None}'),
    ("deny_refused_passes", 'return {"verdict": UNKNOWN, "reason": "deny_refused_acl_unproven"}', 'return {"verdict": PASS, "reason": None}'),
    ("deny_ignores_source_control", "    if source_alive and destination_alive:\n", "    if destination_alive:\n"),
    ("deny_ignores_destination_control", "    if source_alive and destination_alive:\n", "    if source_alive:\n"),
    ("destination_control_always_up", 'return tagged and tcp_probe(kube, pod, str(address), ALLOWED_PORT) == CONNECTED', "return True"),
    ("destination_control_ignores_tag", 'return tagged and tcp_probe', "return tcp_probe"),
    ("absent_peer_probed_anyway", '    if not forbidden:\n        return NO_PEER, True\n',
     '    forbidden = forbidden or [{"ipv4": "100.64.0.2", "tags": []}]\n'),
    ("self_tag_ignored", '    if route["from_tag"] not in first["self_tags"]:\n        return stopped(route, FAIL, "self_tag_mismatch")\n', ""),
    ("peer_tag_ignored", '    if route["peer_tag"] not in peer["tags"]:\n        return stopped(route, FAIL, "peer_tag_mismatch")\n', ""),
    ("peer_name_prefix_match", '{"name": peer["DNSName"].split(".")[0],', '{"name": peer["DNSName"].split(".")[0].removesuffix("-1"),'),
    # MTU.
    ("mtu_exit_code_ignored", "if read.code != 0 or not text.isdigit()", "if not text.isdigit()"),
    ("full_size_not_asked_again", "    if again:\n        return full_size\n    # The floor", "    # The floor"),
    ("ipv4_header_size", '("ipv4", "-4", 28)', '("ipv4", "-4", 48)'),
    ("ipv6_fallback_dropped", '(("ipv4", "-4", 28), ("ipv6", "-6", 48))', '(("ipv4", "-4", 28),)'),
    ("df_bit_not_set", 'kube.exec(pod, "ping", flag, "-M", "do", "-c"', 'kube.exec(pod, "ping", flag, "-M", "dont", "-c"'),
    ("ipv6_header_size", '("ipv6", "-6", 48)', '("ipv6", "-6", 28)'),
    ("black_hole_passes", '"verdict": FAIL if below else PASS,', '"verdict": PASS,'),
    ("no_baseline_needed", "        if not answered:\n            continue\n", ""),
    ("no_reply_reported_as_failure", '    return unknown(reason, interface)',
     '    return {**unknown(reason, interface), "verdict": FAIL}'),
    ("mid_search_failure_hidden", "    if answered is None:\n        return None\n    # Asked again", "    # Asked again"),
    ("second_ask_failure_hidden", "return floor if again is False and send(floor) else None", "return floor if send(floor) else None"),
    ("floor_not_confirmed", "return floor if again is False and send(floor) else None", "return floor if again is False else None"),
    ("ceiling_above_interface", "peer[family], interface - header)", "peer[family], interface)"),
    ("busybox_summary_accepted", r'PING_SUMMARY = re.compile(r"^\d+ packets transmitted, (\d+) received", re.MULTILINE)',
     r'PING_SUMMARY = re.compile(r"^\d+ packets transmitted, (\d+) (?:packets )?received", re.MULTILINE)'),
    # Values reported as read.
    ("online_state_invented", '"peer_online": current.get("online"),', '"peer_online": True,'),
    ("region_reported_for_any_path", "pongs[-1].region if kind == DERP else None", "pongs[-1].region"),
    # The exact Kubernetes objects and a bound on every kubectl call.
    ("wrong_namespace", 'NAMESPACE = "tailscale-system"', 'NAMESPACE = "kube-system"'),
    ("wrong_container", 'CONTAINER = "tailscale"', 'CONTAINER = "proxy"'),
    ("managed_label_dropped", '"tailscale.com/managed": "true", ', ""),
    ("parent_namespace_label_dropped", '        labels["tailscale.com/parent-resource-ns"] = parent_namespace\n', "        pass\n"),
    ("json_output_not_requested", '*args, "-o", "json")', "*args)"),
    ("kubectl_timeout_dropped", "timeout=COMMAND_TIMEOUT_S, ", ""),
    # Read-only boundary.
    ("read_only_list_removed",
     ('        if not any(allowed.fullmatch(" ".join(command)) for allowed in READ_ONLY_POD_COMMANDS):\n'
     '            raise ValueError("refusing a command outside the read-only list")\n'), ""),
    ("read_only_list_prefix_match", "allowed.fullmatch(", "allowed.match("),
    ("tcp_probe_sends_data", '"nc", "-vv", "-z", "-w", "3", address', '"nc", "-vv", "-w", "3", address'),
    # Nothing identifying reaches the report.
    ("address_copied_to_report", '"peer_online": current.get("online"),', '"peer_online": current.get("online"), "address": address,'),
    ("error_text_copied_to_report", 'f"unexpected_{type(error).__name__}"', "str(error)"),
    ("context_copied_to_report", '"check": "tailnet-path", "schema_version": 1,',
     '"check": "tailnet-path", "schema_version": 1, "contexts": contexts,'),
]


def run_suite(tree):
    return subprocess.run(
        [sys.executable, "-m", "unittest", "test_tailnet_path_check"],
        cwd=tree / "scripts/tests", capture_output=True, text=True, timeout=120, check=False,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": ""})


def copy_tree(destination):
    (destination / WORKLOADS).mkdir(parents=True)
    (destination / "scripts/tests").mkdir(parents=True)
    for name in (TOOL, "network.py", "README.md"):
        shutil.copy(ROOT / WORKLOADS / name, destination / WORKLOADS / name)
    shutil.copy(ROOT / "scripts/tests/test_tailnet_path_check.py", destination / "scripts/tests")


class MutationTests(unittest.TestCase):
    def test_unchanged_copy_passes_so_a_failure_below_means_the_mutation(self):
        with tempfile.TemporaryDirectory() as work:
            copy_tree(Path(work))
            result = run_suite(Path(work))
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_every_rule_removed_is_noticed(self):
        source = (ROOT / WORKLOADS / TOOL).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as work:
            copy_tree(Path(work))
            target = Path(work) / WORKLOADS / TOOL
            for name, old, new in MUTATIONS:
                with self.subTest(mutation=name):
                    self.assertEqual(source.count(old), 1, f"{name} no longer matches the source exactly once")
                    target.write_text(source.replace(old, new), encoding="utf-8")
                    result = run_suite(Path(work))
                    self.assertTrue(RESULT_LINE.search(result.stderr), f"{name} survived:\n{result.stderr[-500:]}")


if __name__ == "__main__":
    unittest.main()
