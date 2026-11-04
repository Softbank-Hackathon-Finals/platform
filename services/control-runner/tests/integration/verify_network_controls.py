"""Opt-in Docker transport qualification, not Kubernetes NetworkPolicy evidence.

Two internal bridges, no published ports/host socket. Optional --firewall gives
only the target fixture root + NET_ADMIN in its own network namespace; agents
remain unprivileged. Never use this fixture as a production app deployment.
Pass a locally built, locked-dependency Python image by immutable sha256 ID.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bravo_controls.network_probes import (
    Attempt,
    Endpoint,
    NetworkAccessProbe,
    NetworkTarget,
    PolicySnapshot,
)
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

TCP = """import json,sys
from dataclasses import asdict, replace
from bravo_controls.network_probes import Endpoint,SocketTCPRunner
agent,ip,port,nonce=sys.argv[1:]
e=Endpoint(ip,int(port));r=SocketTCPRunner(agent=agent,endpoints=frozenset({e}))(agent,e,nonce)
print(json.dumps(asdict(r),default=lambda d:d.isoformat()))
"""
DNS = """import json,sys,socket,signal,time
from datetime import datetime,UTC
from dataclasses import asdict, replace
from bravo_controls.network_probes import Attempt
agent,name,nonce=sys.argv[1:]
def timeout(*args):raise TimeoutError()
signal.signal(signal.SIGALRM,timeout);signal.alarm(3)
start=time.monotonic();outcome='error'
try:
 if socket.getaddrinfo(name,8080):outcome='resolved'
except (OSError,TimeoutError):pass
finally:signal.alarm(0)
print(json.dumps(asdict(Attempt(nonce,agent,name,outcome,round((time.monotonic()-start)*1000),datetime.now(UTC))),default=lambda d:d.isoformat()))
"""
SERVER = """import socket,sys,select
servers=[]
for port in sys.argv[1].split(','):
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',int(port)));s.listen(8);servers.append(s)
while True:
 for s in select.select(servers,[],[])[0]:
  c,_=s.accept();c.close()
"""


def command(*args, timeout=30):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def run(output, image, firewall=False):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable local test image ID required")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    source = Path(__file__).resolve().parents[2] / "src"
    suffix = uuid4().hex[:12]
    nets = ["bravo-net-" + suffix + "-" + x for x in ("target", "probe")]
    names = {x: "bravo-net-" + suffix + "-" + x for x in ("target", "health", "allowed", "denied")}
    created = []
    result = {
        "test_scope": "Docker internal bridges; not AWS or Kubernetes policy qualification",
        "image": image,
        "firewall_fixture": firewall,
    }
    try:
        for net in nets:
            command("docker", "network", "create", "--internal", net)
            created.append(("network", net))
        ids = {}
        for role in names:
            net = nets[0] if role in ("target", "allowed") else nets[1]
            args = [
                "docker",
                "run",
                "-d",
                "--name",
                names[role],
                "--network",
                net,
                "--network-alias",
                "control" if role == "health" else role,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user",
                "0:0" if firewall and role == "target" else f"{os.getuid()}:{os.getgid()}",
                "--memory",
                "96m",
                "--cpus",
                "0.5",
                "--pids-limit",
                "32",
                "-v",
                f"{source}:/src:ro",
                "-e",
                "PYTHONPATH=/src",
                "--entrypoint",
                "/probe/.venv/bin/python",
                image,
            ]
            if firewall and role == "target":
                args[args.index(image) : args.index(image)] = ["--cap-add=NET_ADMIN"]
            args += (
                ["-c", SERVER, "5432,6432" if role == "target" else "8080"]
                if role in ("target", "health")
                else ["-c", "import time; time.sleep(600)"]
            )
            ids[role] = command(*args).strip()
            created.append(("container", names[role]))

        def ip(role, net):
            return json.loads(command("docker", "inspect", names[role]))[0]["NetworkSettings"][
                "Networks"
            ][net]["IPAddress"]

        if firewall:
            command("docker", "network", "connect", nets[0], names["denied"])
            denied_ip = ip("denied", nets[0])

        def firewall_rule(action):
            command(
                "docker",
                "exec",
                names["target"],
                "iptables",
                action,
                "INPUT",
                "-p",
                "tcp",
                "-s",
                denied_ip,
                "-m",
                "multiport",
                "--dports",
                "5432,6432",
                "-j",
                "DROP",
            )

        if firewall:
            firewall_rule("-A")
            result["iptables_version"] = command(
                "docker", "exec", names["target"], "iptables", "--version"
            ).strip()

        ctx = RunContext(
            "local-connectivity",
            "demo-chat",
            Environment("local-test", "target", "docker:internal-bridges"),
        )
        target = NetworkTarget(
            ctx.app,
            ctx.environment,
            ids["target"],
            Endpoint(ip("target", nets[0]), 5432),
            ids["allowed"],
            ids["denied"],
            Endpoint(ip("health", nets[1]), 8080),
            "control",
        )

        def tcp(agent, endpoint, nonce):
            raw = command(
                "docker",
                "exec",
                agent,
                "/probe/.venv/bin/python",
                "-c",
                TCP,
                agent,
                endpoint.ip,
                str(endpoint.port),
                nonce,
                timeout=5,
            )
            value = json.loads(raw)
            value["observed_at"] = datetime.fromisoformat(value["observed_at"])
            return Attempt(**value)

        def dns(agent, name, nonce):
            raw = command(
                "docker",
                "exec",
                agent,
                "/probe/.venv/bin/python",
                "-c",
                DNS,
                agent,
                name,
                nonce,
                timeout=5,
            )
            value = json.loads(raw)
            value["observed_at"] = datetime.fromisoformat(value["observed_at"])
            return Attempt(**value)

        def policy(c, bound_target):
            docs = json.loads(command("docker", "network", "inspect", *nets))
            current = json.loads(command("docker", "inspect", *names.values()))
            if {item["Id"] for item in current} != set(ids.values()):
                raise ValueError("registered container identity changed")
            if firewall:
                docs.append({"rules": command("docker", "exec", names["target"], "iptables", "-S")})
            digest = (
                "sha256:" + hashlib.sha256(json.dumps(docs, sort_keys=True).encode()).hexdigest()
            )
            return PolicySnapshot(c, bound_target.binding_sha256, digest, datetime.now(UTC))

        spec = Spec("NET-01.3", "NET-01", "1", "deny", "required")
        evidence = []

        def save(data):
            evidence.append(data)
            (output / f"evidence-{len(evidence) - 1}.json").write_bytes(data)

        probe = NetworkAccessProbe(
            targets=(target, replace(target, endpoint=Endpoint(target.endpoint.ip, 6432))),
            run_tcp=tcp,
            run_dns=dns,
            read_policy=policy,
            evidence_sink=save,
        )
        deadline = time.monotonic() + 15
        while tcp(ids["allowed"], target.endpoint, "ready").outcome != "connected":
            if time.monotonic() > deadline:
                raise TimeoutError("target startup")
            time.sleep(0.2)
        closed = probe(ctx, spec)
        result["isolated_observed"] = closed.observed
        result["isolated_reason"] = closed.unknown_reason
        print(json.dumps({"initial": asdict(closed)}, default=str), flush=True)
        assert closed.positive_control == "pass"
        assert closed.observed in {"deny", None}
        if firewall:
            assert closed.observed == "deny"
            assert json.loads(evidence[-2])["observations"]["probe"]["outcome"] == "timeout"
            result["actual_syn_drop_is_denied_with_controls"] = True
        if closed.observed is None:
            assert closed.unknown_reason == "UNEXPECTED_RESPONSE"
        result["isolated_path_never_reported_allow"] = True
        result["isolated_transport_outcome"] = json.loads(evidence[-2])["observations"]["probe"][
            "outcome"
        ]
        # Opening only our disposable bridge makes the negative attempt succeed.
        if firewall:
            firewall_rule("-D")
        else:
            command("docker", "network", "connect", nets[0], names["denied"])
        opened = probe(ctx, spec)
        assert opened.observed == "allow"
        result["opened_path_is_allow_not_pass"] = True
        if firewall:
            firewall_rule("-A")
        else:
            command("docker", "network", "disconnect", nets[0], names["denied"])
        # Loss of either required target control must be UNKNOWN, never a denial PASS.
        if firewall:
            command(
                "docker",
                "exec",
                names["target"],
                "iptables",
                "-A",
                "INPUT",
                "-p",
                "tcp",
                "-m",
                "multiport",
                "--dports",
                "5432,6432",
                "-j",
                "REJECT",
            )
            result["target_fault_kind"] = "allowed_path_rejected"
        else:
            command("docker", "stop", "-t", "1", names["target"])
            result["target_fault_kind"] = "server_stopped"
        unavailable = probe(ctx, spec)
        assert (
            unavailable.observed is None and unavailable.unknown_reason == "POSITIVE_CONTROL_FAILED"
        )
        result["unavailable_target_is_unknown"] = True
        if firewall:
            command(
                "docker",
                "exec",
                names["target"],
                "iptables",
                "-D",
                "INPUT",
                "-p",
                "tcp",
                "-m",
                "multiport",
                "--dports",
                "5432,6432",
                "-j",
                "REJECT",
            )
        else:
            command("docker", "start", names["target"])
        deadline = time.monotonic() + 15
        while tcp(ids["allowed"], target.endpoint, "ready-again").outcome != "connected":
            if time.monotonic() > deadline:
                raise TimeoutError("target restart")
            time.sleep(0.2)
        command("docker", "stop", "-t", "1", names["health"])
        unhealthy = probe(ctx, spec)
        assert unhealthy.observed is None and unhealthy.unknown_reason == "POSITIVE_CONTROL_FAILED"
        result["unhealthy_probe_control_is_unknown"] = True
        result["timestamp"] = datetime.now(UTC).isoformat()
        result["module_sha256"] = hashlib.sha256(
            (source / "bravo_controls/network_probes.py").read_bytes()
        ).hexdigest()
        result["evidence_count"] = len(evidence)
        for index, data in enumerate(evidence):
            (output / f"evidence-{index}.json").write_bytes(data)
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        for kind, name in reversed(created):
            command("docker", kind, "rm", *(["-f"] if kind == "container" else []), name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--firewall",
        action="store_true",
        help="Target fixture only: root + NET_ADMIN inside own container, no host network",
    )
    args = parser.parse_args()
    run(args.output.resolve(), args.image, args.firewall)
