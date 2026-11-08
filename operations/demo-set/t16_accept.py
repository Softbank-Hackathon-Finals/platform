"""S1 COLLECT -> ANALYZE: hand one fresh read-only T16 scan to T10 for a registered plan.

Usage (from this directory):
  python3 operations/demo-set/t16_accept.py <plan-id> <scan-dir-name> [--command-key K]
Reads <workdir>/<plan>/{new-plan-receipt.json,<scan-dir>/}, the T16 reader config and the workload receipt.
Checks (as the original operator intake): scan PASS and < 30 min old, file hashes, inventory schema and
digest, app/source/scope binding, exact metadata-only API set, every write probe denied. Then, inside the
current single Ready t10-registration Pod (the demo config's platform kubeconfig), stores the
original once and completes collection (version 1 -> ANALYZE). Re-running returns the stored receipt.
"""
import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import demo_config

CFG = demo_config.load()
RUNS = CFG["workdir"]  # <workdir>/<plan-id>/...
EXPECTED_CALLS = {"file:clusters.json", "file:deployments.json", "file:serviceaccounts.json", "iam:GetRolePolicy",
                  "iam:ListAttachedRolePolicies", "iam:ListRolePolicies", "kms:DescribeKey", "s3:GetBucketLocation",
                  "s3:GetBucketPolicyStatus", "secretsmanager:DescribeSecret"}

p = argparse.ArgumentParser()
p.add_argument("plan")
p.add_argument("scan")
p.add_argument("--command-key", default=None)
a = p.parse_args()
run = RUNS / a.plan
scan = run / a.scan
r = json.loads((scan / "result.json").read_text())
inv = json.loads((scan / "inventory.json").read_text())
cfg = json.loads(Path(CFG["t16"]["reader_config"]).read_text())
plan = json.loads((run / "new-plan-receipt.json").read_text())
app = json.loads(Path(CFG["workload_receipt"]).read_text())

assert r["status"] == "PASS" and r["scan_started"] is True and r["scan_exit_code"] == 0
assert r["credentials_saved"] is False
assert r["personal_identity"]["UserId"] in cfg["allowed_sso_user_ids"]
assert r["reader_identity"]["UserId"].split(":")[0] == cfg["reader_role_id"]
assert (datetime.now(UTC) - datetime.fromisoformat(r["ended_at"])).total_seconds() < 1800, "scan_too_old"
assert hashlib.sha256((scan / "inventory.json").read_bytes()).hexdigest() == r["inventory_sha256"]
for name, sha in r["export_sha256"].items():
    assert hashlib.sha256((scan / "exports" / name).read_bytes()).hexdigest() == sha
body = {k: v for k, v in inv.items() if k not in {"digest", "generated_at"}}
assert inv["digest"] == "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                               ensure_ascii=False, allow_nan=False).encode()).hexdigest()
assert inv["app"]["id"] == plan["workload_id"] and inv["app"]["source_sha"] == app["scope"]["source_sha"] == cfg["source_sha"]
assert inv["source"]["env_id"] == plan["source_environment_id"] and inv["source"]["namespace"] == app["scope"]["namespace"]
assert set(inv["inputs"]["api_calls"]) == EXPECTED_CALLS
assert all(x.get("http", 200) == 200 and x.get("allowed", False) is False and x.get("unauthenticated_http", 401) == 401
           for x in r["checks"])

data = {"plan": plan, "app": app, "inventory": inv, "reader_receipt": r, "recorded_by": r["personal_identity"]["Arn"],
        "evidence_id": "t16-" + r["inventory_sha256"], "command_key": a.command_key or "t16-source-" + plan["plan_id"][-12:],
        "run_id": "t16-source-" + r["ended_at"][:16].replace(":", ""), "occurred_at": r["ended_at"]}
INNER = r'''
import json,sys
from bravo_inventory.report import validate_inventory
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.window_server import runtime_connection
from bravo_platform_events.plans import CollectionProof
from bravo_platform_events.registration import Registry,digest
from bravo_platform_events.cutover import _get,_put
D=json.loads(sys.stdin.read())
validate_inventory(D['inventory'])
store=PostgresPlanStore(runtime_connection())
plan_id=D['plan']['plan_id'];key='collection-original:'+D['evidence_id']
with store._transaction(write=True,plan_id=plan_id) as c:
    row=c.execute('SELECT * FROM plans WHERE plan_id=?',(plan_id,)).fetchone()
    assert row and row['tenant_id']=='bravo'
    spec=json.loads(row['spec_json'])
    assert all(spec[k]==D['plan'][k] for k in ['plan_digest','collection_scope_digest','workload_id','source_environment_id','target_environment_id','profile_id','profile_version'])
    app=Registry._read(c,row['tenant_id'],'workload',spec['workload_id'])
    assert digest(app['scope'])==spec['collection_scope_digest']
    record={'evidence_id':D['evidence_id'],'plan_id':plan_id,'plan_digest':spec['plan_digest'],'collection_scope_digest':spec['collection_scope_digest'],'inventory':D['inventory'],'reader_receipt':D['reader_receipt'],'recorded_by':D['recorded_by']}
    if _get(c,plan_id,key) is None: _put(c,plan_id,key,{},record)
    assert _get(c,plan_id,key)[1]==record
proof=CollectionProof(plan_id,D['plan']['plan_digest'],D['plan']['collection_scope_digest'],D['evidence_id'],True)
receipt=store.complete_collection(proof,expected_version=1,command_key=D['command_key'],run_id=D['run_id'],occurred_at=D['occurred_at'])
with store._transaction() as c:
    row=dict(c.execute('SELECT state,version FROM plans WHERE plan_id=?',(plan_id,)).fetchone())
print(json.dumps({'status':'PASS','collection':receipt,'readback':row,'evidence_id':D['evidence_id']}))
'''
kc = demo_config.kubeconfig(CFG, "platform")
K = ["kubectl", "--kubeconfig", kc, "-n", "platform-state"]
assert subprocess.run(["kubectl", "--kubeconfig", kc, "get", "ns", "kube-system", "-o", "jsonpath={.metadata.uid}"],
                      capture_output=True, text=True).stdout == "5d978ed5-52a9-405a-a4ce-fd2e1c3ac9f9", "platform_identity"
dep = json.loads(subprocess.run(K + ["get", "deploy", "t10-registration", "-o", "json"], capture_output=True, text=True, check=True).stdout)
sel = ",".join(k + "=" + v for k, v in dep["spec"]["selector"]["matchLabels"].items())
pods = [x for x in json.loads(subprocess.run(K + ["get", "pods", "-l", sel, "-o", "json"], capture_output=True, text=True, check=True).stdout)["items"]
        if not x["metadata"].get("deletionTimestamp") and all(s.get("ready") for s in x["status"].get("containerStatuses", []))]
assert len(pods) == 1, "single_ready_registration_pod_required"
pod = pods[0]["metadata"]["name"]
script = "import sys;exec(" + repr(INNER) + ")"
out = subprocess.run(K + ["exec", "-i", pod, "-c", "registration", "--", "python", "-c", script], input=json.dumps(data),
                     capture_output=True, text=True, timeout=120)
print(out.stdout.strip().splitlines()[-1] if out.stdout.strip() else json.dumps({"status": "FAIL", "err": out.stderr.strip()[-400:]}))
