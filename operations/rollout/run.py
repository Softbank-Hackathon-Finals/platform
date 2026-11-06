"""One-shot platform rollout: pause merges, verify the target, merge pins, apply bootstrap, read back.

    python3 operations/rollout/run.py --instance-id i-… --cluster-uid <kube-system uid> \
        --runtime-values /private/runtime-values.json --reader-adoption /private/adoption.json \
        --pin-pr 563 --pin-pr 564 --pin-pr 562 [--apply] > rollout.json

Every step reads back before the next. Without --apply it only checks the target and runs the
bootstrap plan (no merge, no pause, no mutation). The Reader reads v1 and v2 configs, so pins and
config may land in any order; no Argo auto-sync pause is needed. The merge-queue pause is always
restored, even when a step fails. Output is one JSON object; exit 0 only when every step passed.
Runtime values and adoption carry names and UIDs only (no secret values); they travel in the SSM
command and stay outside the checkout on the node.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = "Softbank-Hackathon-Finals/platform"
QUEUE_VARIABLE = "MERGE_QUEUE_BOT_ENABLED"
NODE = r'''
import json,subprocess,sys,os,hashlib
from pathlib import Path
d=json.load(sys.stdin)
K=['/var/lib/rancher/rke2/bin/kubectl','--kubeconfig=/etc/rancher/rke2/rke2.yaml','--request-timeout=30s']
def kj(*a): return json.loads(subprocess.run(K+list(a)+['-o','json'],capture_output=True,text=True,timeout=60,check=True).stdout)
def run(a,**k):
    p=subprocess.run(a,capture_output=True,text=True,**k)
    if p.returncode: raise RuntimeError(a[0].rsplit('/',1)[-1]+':'+p.stderr[-300:])
    return p.stdout
out={'cluster_uid':kj('get','namespace','kube-system')['metadata']['uid']}
if out['cluster_uid']!=d['cluster_uid']: print(json.dumps({'status':'FAIL','reason':'cluster_identity_mismatch'})); sys.exit(0)
if d['op']=='identity': print(json.dumps({'status':'PASS',**out})); sys.exit(0)
if d['op']=='readback':
    rows={}
    for name in ('t10-product-reader','t10-registration','t10-workflow-host'):
        try:
            dep=kj('-n','platform-state','get','deploy',name)
            rows[name]={'image':dep['spec']['template']['spec']['containers'][0]['image'],'ready':dep['status'].get('readyReplicas',0),
                        'config_sha256':dep['spec']['template']['metadata'].get('annotations',{}).get('bravo.dev/config-sha256')}
        except subprocess.CalledProcessError: rows[name]=None
    print(json.dumps({'status':'PASS','workloads':rows,**out})); sys.exit(0)
rev=d['revision']; root=Path('/var/lib/bravo/gitops/checkouts')/rev
if not root.exists():
    root.mkdir(parents=True)
    for a in (['git','init',str(root)],['git','-C',str(root),'remote','add','origin','https://github.com/'+d['repo']+'.git'],
              ['git','-C',str(root),'fetch','--depth=1','origin',rev],['git','-C',str(root),'checkout','--detach',rev]): run(a,timeout=120)
if run(['git','-C',str(root),'rev-parse','HEAD']).strip()!=rev or run(['git','-C',str(root),'status','--porcelain']).strip(): raise SystemExit('checkout_mismatch')
out_dir=Path('/var/lib/bravo/gitops/rollout')/rev; out_dir.mkdir(parents=True,exist_ok=True); os.umask(0o077)
def keep(name,value):
    p=out_dir/(name+'-'+hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:12]+'.json')
    if not p.exists(): p.write_text(json.dumps(value,sort_keys=True))
    return str(p)
reg=kj('-n','platform-state','get','deploy','t10-registration')['metadata']['uid']
a=['python3',str(root/'infra/gitops/bootstrap.py'),'--cluster-uid',d['cluster_uid'],'--deployment-uid',reg,'--revision',rev,
   '--kubectl',K[0],'--kubeconfig','/etc/rancher/rke2/rke2.yaml','--runtime-values',keep('values',d['values'])]
if d.get('adoption'): a+=['--reader-adoption',keep('adoption',d['adoption'])]
if d['op']=='apply': a.append('--apply')
env=dict(os.environ); env['PATH']='/var/lib/bravo/gitops/tools/helm-v4.0.0:'+env.get('PATH','')
print(json.dumps({'status':'PASS','bootstrap':json.loads(run(a,timeout=240,env=env)),**out}))
'''


class Failure(Exception):
    pass


def sh(args, *, check=True):
    p = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and p.returncode:
        raise Failure(args[0] + ":" + p.stderr.strip()[-200:])
    return p.stdout.strip()


def node(instance_id, packet, timeout=300):
    """Run NODE on the platform node through SSM; the packet travels on stdin of the command."""
    source = base64.b64encode(NODE.encode()).decode()
    command = ("python3 -c \"import base64;exec(base64.b64decode('" + source + "'))\" <<'BRAVO_PACKET'\n"
               + json.dumps(packet) + "\nBRAVO_PACKET")
    sent = json.loads(sh(["aws", "ssm", "send-command", "--region", "ap-northeast-2", "--instance-ids", instance_id,
                          "--document-name", "AWS-RunShellScript", "--comment", "bravo rollout " + packet["op"],
                          "--parameters", json.dumps({"commands": [command], "executionTimeout": [str(timeout)]}),
                          "--output", "json"]))["Command"]["CommandId"]
    for _ in range(timeout // 3 + 10):
        time.sleep(3)
        got = json.loads(sh(["aws", "ssm", "get-command-invocation", "--region", "ap-northeast-2", "--command-id", sent,
                             "--instance-id", instance_id, "--output", "json"], check=False) or "{}")
        if got.get("Status") in ("Success", "Failed", "TimedOut", "Cancelled"):
            lines = [line for line in got.get("StandardOutputContent", "").splitlines() if line.startswith("{")]
            if not lines:
                raise Failure("node_" + packet["op"] + "_" + got["Status"])
            result = json.loads(lines[-1])
            if result.get("status") != "PASS":
                raise Failure("node_" + packet["op"] + ":" + str(result.get("reason")))
            return result
    raise Failure("node_" + packet["op"] + "_unconfirmed")


def merged(pr, wait):
    sh(["gh", "pr", "merge", str(pr), "-R", REPO], check=False)  # queue entry; already queued is fine
    for _ in range(wait // 20):
        state = sh(["gh", "pr", "view", str(pr), "-R", REPO, "--json", "state,mergeCommit",
                    "--jq", '.state + " " + (.mergeCommit.oid // "")'])
        if state.startswith("MERGED"):
            return state.split()[1]
        if state.startswith("CLOSED"):
            raise Failure(f"pin_pr_{pr}_closed")
        time.sleep(20)
    raise Failure(f"pin_pr_{pr}_not_merged")


def pins(revision):
    out = {}
    for service in ("registration", "product-reader", "rehearsal-host"):
        raw = sh(["gh", "api", f"repos/{REPO}/contents/infra/gitops/environments/development/{service}-image.json?ref={revision}",
                  "--jq", ".content"])
        out[service] = json.loads(base64.b64decode(raw))["image"]["digest"]
    return out


def rollout(a, record):
    def step(name, fn):
        record["steps"].append({"step": name, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        value = fn()
        record["steps"][-1]["result"] = value
        return value

    step("target_identity", lambda: node(a.instance_id, {"op": "identity", "cluster_uid": a.cluster_uid}))
    values = json.loads(Path(a.runtime_values).read_text())
    adoption = json.loads(Path(a.reader_adoption).read_text()) if a.reader_adoption else None
    if a.apply:
        previous = sh(["gh", "variable", "get", QUEUE_VARIABLE, "-R", REPO], check=False) or "true"
        record["queue_variable_before"] = previous
        step("pause_merges", lambda: sh(["gh", "variable", "set", QUEUE_VARIABLE, "-R", REPO, "--body", "false"]) or "false")
        try:
            for pr in a.pin_pr:
                step(f"merge_pin_{pr}", lambda pr=pr: merged(pr, a.merge_wait))
            revision = sh(["gh", "api", f"repos/{REPO}/commits/develop", "--jq", ".sha"])
            packet = {"repo": REPO, "cluster_uid": a.cluster_uid, "revision": revision, "values": values, "adoption": adoption}
            step("bootstrap_plan", lambda: node(a.instance_id, packet | {"op": "plan"}))
            step("bootstrap_apply", lambda: node(a.instance_id, packet | {"op": "apply"}))
            expected = pins(revision)
            record["expected_digests"] = expected
            for _ in range(a.ready_wait // 15):
                time.sleep(15)
                rows = node(a.instance_id, {"op": "readback", "cluster_uid": a.cluster_uid})["workloads"]
                checks = {name: bool(row) and row["ready"] >= 1 and row["image"].endswith(expected[service])
                          for name, service in (("t10-product-reader", "product-reader"),
                                                ("t10-registration", "registration"),
                                                ("t10-workflow-host", "registration")) if (row := rows.get(name)) is not None}
                if checks and all(checks.values()):
                    break
            record["steps"].append({"step": "readback", "result": rows, "checks": checks})
            if not checks or not all(checks.values()):
                raise Failure("workloads_not_ready_on_pinned_digests")
        finally:
            sh(["gh", "variable", "set", QUEUE_VARIABLE, "-R", REPO, "--body", record["queue_variable_before"]], check=False)
            record["queue_variable_restored"] = record["queue_variable_before"]
    else:
        revision = sh(["gh", "api", f"repos/{REPO}/commits/develop", "--jq", ".sha"])
        step("bootstrap_plan", lambda: node(a.instance_id, {"op": "plan", "repo": REPO, "cluster_uid": a.cluster_uid,
                                                            "revision": revision, "values": values, "adoption": adoption}))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance-id", required=True)
    p.add_argument("--cluster-uid", required=True)
    p.add_argument("--runtime-values", required=True)
    p.add_argument("--reader-adoption")
    p.add_argument("--pin-pr", type=int, action="append", default=[])
    p.add_argument("--merge-wait", type=int, default=1800)
    p.add_argument("--ready-wait", type=int, default=300)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    if not re.fullmatch(r"i-[0-9a-f]{17}", a.instance_id) or not re.fullmatch(r"[0-9a-f-]{36}", a.cluster_uid):
        p.error("exact instance and cluster UID required")
    record = {"kind": "bravo-rollout.v1", "apply": a.apply, "pins": a.pin_pr, "steps": []}
    try:
        rollout(a, record)
        record["result"] = "PASS"
    except Failure as error:
        record["result"], record["reason"] = "FAIL", str(error)
    print(json.dumps(record, indent=1))
    return 0 if record["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
