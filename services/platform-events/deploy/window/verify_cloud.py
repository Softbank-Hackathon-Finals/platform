"""Operator probe through a temporary tag:bravo-loadgen tailnet device.

Run on the RKE2 host after migration, synthetic enrollment and receiver readiness.
Stdin is the public SliWindow fixture. Tokens stay in host memory / exec stdin.
This verifies the real tailnet/HTTP/DB path, not the T18 owner's EC2 or app load.
It revokes the synthetic binding on success. The operator removes probe resources
with UID preconditions after retaining the receipt. No source/target DB is touched.
"""

import base64
import hashlib
import json
import selectors
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

K = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig", "/etc/rancher/rke2/rke2.yaml",
     "--request-timeout=20s"]
NS = "platform-state"
NAME = "t10-window-intake"


def run(args, data=None, timeout=30):
    result = subprocess.run(K + args, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("probe_operation_failed")
    return result.stdout


def get(kind, name, ns=NS):
    return json.loads(run(["get", kind, name, "-n", ns, "-o", "json"]))


def main():
    document = json.load(sys.stdin)
    secret = get("secret", "t10-window-producer-identity")
    identity = json.loads(base64.b64decode(secret["data"]["producer.json"]))["identity"]
    assert identity["plan_id"].startswith("t10-ingress-probe-")
    token = base64.b64decode(get("secret", "t10-window-producer-token")["data"]["token"]).decode()
    proxy = json.loads(run(["get", "pods", "-n", "tailscale-system", "-l",
        "tailscale.com/parent-resource=" + NAME, "-o", "json"]))["items"]
    assert len(proxy) == 1 and all(c["ready"] for c in proxy[0]["status"]["containerStatuses"])
    peer = proxy[0]["metadata"]["name"]
    status = json.loads(run(["exec", "-n", "tailscale-system", peer, "-c", "tailscale",
                             "--", "tailscale", "status", "--json"]))
    address = status["Self"]["TailscaleIPs"][0]
    assert status["Self"]["Tags"] == ["tag:bravo-window-intake"]
    client_status = json.loads(run(["exec", "-n", "tailscale-system", "t10-window-probe-0",
        "-c", "tailscale", "--", "tailscale", "status", "--json"]))
    assert client_status["Self"]["Tags"] == ["tag:bravo-loadgen"]
    denied_pod = get("pod", "bravo-cutover-control-0", "tailscale-system")
    assert all(c["ready"] for c in denied_pod["status"]["containerStatuses"])
    denied_status = json.loads(run(["exec", "-n", "tailscale-system", "bravo-cutover-control-0",
        "-c", "tailscale", "--", "tailscale", "status", "--json"]))
    assert denied_status["BackendState"] == "Running"
    assert denied_status["Self"]["Tags"] == ["tag:bravo-cutover-control"]
    run(["exec", "-n", "tailscale-system", "bravo-cutover-control-0", "-c", "tailscale", "--", "nc", "--help"])
    now = datetime.now(UTC)
    document.update(plan_id=identity["plan_id"], received_at="2000-01-01T00:00:00Z")
    document["window"] = {"start": (now - timedelta(seconds=11)).isoformat(),
                          "end": (now - timedelta(seconds=1)).isoformat()}

    def request(payload, bearer, source="t10-window-probe-0"):
        body = json.dumps(payload).encode()
        header = (f"POST /v1/observability/windows HTTP/1.1\r\nHost: window-intake\r\n"
                  f"Authorization: Bearer {bearer}\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        started = time.monotonic()
        process = subprocess.Popen(K + ["exec", "-i", "-n", "tailscale-system", source,
            "-c", "tailscale", "--", "nc", "-w", "4", address, "30981"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # BusyBox nc exits at stdin EOF. Keep it open until the HTTP status
            # arrives; otherwise a committed request may lose its acknowledgement.
            process.stdin.write(header + body)
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                first = process.stdout.readline() if selector.select(8) else b""
            process.stdin.close()
            process.wait(timeout=8)
            stderr = process.stderr.read()
            elapsed = time.monotonic() - started
            if first.startswith(b"HTTP/1.1 "):
                return int(first.split()[1])
            if (source == "bravo-cutover-control-0" and process.returncode == 1
                    and 3.5 <= elapsed < 10 and stderr.strip() == b"command terminated with exit code 1"):
                return "tcp_timeout"
            raise RuntimeError("transport_unverified")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    long_time = document["window"]["start"].split(".")[0] + "." + "0" * 64 + "+00:00"
    malformed = document | {"window": document["window"] | {"start": long_time}}
    checks = {"long_timestamp": request(malformed, token),
              "accepted": request(document, token), "duplicate": request(document, token),
              "wrong_token": request(document, "invalid-test-token-" * 3),
              "other_plan": request(document | {"plan_id": "other-plan"}, token),
              "other_tag": request(document, token, "bravo-cutover-control-0")}
    assert checks == {"long_timestamp": 422, "accepted": 201, "duplicate": 409, "wrong_token": 401,
                      "other_plan": 404, "other_tag": "tcp_timeout"}, checks

    def receiver(command):
        return json.loads(run(["exec", "-i", "deploy/" + NAME, "-n", NS, "--", "python", "-"], command.encode()))

    setup = '''import json
from pathlib import Path
from bravo_platform_events.window_store import ProducerIdentity, WindowIntake
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.window_server import runtime_connection
i=ProducerIdentity(**json.loads(Path('/producer/producer.json').read_text())['identity'])
s=PostgresPlanStore(runtime_connection());w=WindowIntake(s)
'''
    read = setup + '''with s._transaction() as c:
 d=w.latest_in_transaction(c,i.plan_id)
 print(json.dumps(d))
'''
    stored = receiver(read)
    assert stored["plan_id"] == identity["plan_id"] and stored["window"] == document["window"]
    assert stored["received_at"] != document["received_at"]
    privileges = receiver(setup + '''import psycopg
checks={}
for key,sql in {'ddl':'CREATE TABLE bravo_platform.t10_forbidden (id int)',
 'window_update':"UPDATE bravo_platform.sli_windows SET document_json='{}' WHERE false",
 'journal_delete':'DELETE FROM bravo_platform.plan_commands WHERE false'}.items():
 with psycopg.connect(runtime_connection()) as c:
  try:
   c.execute(sql)
  except psycopg.errors.InsufficientPrivilege:
   checks[key]=True
  else:
   checks[key]=False
  c.rollback()
print(json.dumps(checks))
''')
    assert privileges == {"ddl": True, "window_update": True, "journal_delete": True}
    run(["rollout", "restart", "deployment/" + NAME, "-n", NS])
    run(["rollout", "status", "deployment/" + NAME, "-n", NS, "--timeout=90s"], timeout=100)
    assert receiver(read) == stored
    revoked = receiver(setup + '''operation='t10-ingress-revoke'
r=w.set_binding(i,enabled=False,expected_revision=1,
 administration_ref='operator-t10-ingress-probe',command_key=operation)
print(json.dumps({'revision':r['revision'],'enabled':r['enabled']}))
''')
    assert revoked == {"revision": 2, "enabled": False}
    checks["revoked"] = request(document, token)
    assert checks["revoked"] == 403
    print(json.dumps({"status": "PASS", "http": checks, "stored_readback": True,
        "pod_restart_preserved_window": True, "received_at_server_owned": True,
        "synthetic_binding_revoked": True, "actual_t18_ec2_tested": False,
        "runtime_permission_denials": privileges,
        "denied_peer_ready_and_tag_verified": True,
        "document_sha256": hashlib.sha256(json.dumps(stored, sort_keys=True).encode()).hexdigest()}))


if __name__ == "__main__":
    main()
