"""Demo step 3: S1 security baseline for one plan (T19 + T14), the operator's one pass before PROPOSE.

    python3 operations/demo-set/s1.py <plan-id>

enroll the operator gate keys -> T14 target run -> T14 source run -> record both -> bind -> subjects ->
sign (confirm = the fingerprint `subjects` printed) -> capture --evaluate. The demo plan's open bucket makes
the S1 GATE BLOCK on IAM-01.1 by design; that moves the plan to PROPOSE. Outputs: <workdir>/<plan-id>/s1-*.json.
"""

import json
import os
import subprocess
import sys

import demo_config

cfg = demo_config.load()
plan_id = sys.argv[1]
out = demo_config.plan_dir(cfg, plan_id)
demo_config.check(cfg, "platform")
here = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ, KUBECONFIG=demo_config.kubeconfig(cfg, "platform"))


def gate(*args):
    r = subprocess.run(["uv", "run", "--quiet", "--directory", "services/control-gate", "python",
                        "../../operations/gate/run.py", *args, "--plan-id", plan_id,
                        "--context", cfg["clusters"]["platform"]["context"]],
                       cwd=cfg["repo"], env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    value, lines = {"status": "FAILED", "reason": r.stderr[-300:]}, r.stdout.strip().splitlines()
    for i in range(len(lines) - 1, -1, -1):  # the last JSON object, one line or indented
        if lines[i].startswith("{"):
            try:
                value = json.loads("\n".join(lines[i:]))
                break
            except ValueError:
                continue
    (out / f"s1-{args[0]}.json").write_text(json.dumps(value, indent=1))
    print(args[0], value.get("status"), value.get("reason", ""), flush=True)
    if value.get("status") not in ("PASS", "DONE", "READY", None):
        raise SystemExit(f"{args[0]} stopped: {json.dumps(value)[:400]}")
    return value


spec = json.loads(demo_config.kubectl(cfg, "platform", "-n", "platform-state", "exec", "-i", "deploy/t10-workflow-host",
    "--", "python", "-c", "import json,sys\nfrom bravo_platform_events.postgres import PostgresPlanStore\n"
    "from bravo_platform_events.window_server import runtime_connection\ns=PostgresPlanStore(runtime_connection())\n"
    f"with s._transaction() as c:\n print(c.execute('SELECT spec_json FROM plans WHERE plan_id=?',('{plan_id}',))"
    ".fetchone()['spec_json'])").strip().splitlines()[-1])
app, source, target = spec["workload_id"], spec["source_environment_id"], spec["target_environment_id"]
print(json.dumps({"plan": plan_id, "app": app, "source": source, "target": target}), flush=True)
gate("enroll", "--dir", os.path.expanduser(cfg["gate_keys"]), "--valid-hours", "48")
runs = {}
for side, environment in (("target", target), ("source", source)):
    r = subprocess.run([sys.executable, os.path.join(here, "run_controls.py"), str(out), side, app, environment],
                       capture_output=True, text=True, timeout=900)
    summary = json.loads(r.stdout.strip().splitlines()[-1])
    print(json.dumps(summary["verdicts"]), side, flush=True)
    runs[side] = summary["dir"]
for side, directory in runs.items():
    gate("record", "--side", side, "--run", os.path.join(directory, "control-run.json"))
gate("bind")
subjects = gate("subjects")
confirm = subjects.get("confirm") or subjects.get("subjects", {}).get("confirm")
gate("sign", "--dir", os.path.expanduser(cfg["gate_keys"]), "--confirm", confirm)
result = gate("capture", "--evaluate", "--dir", os.path.expanduser(cfg["gate_keys"]))
print(json.dumps({"status": "PASS", "plan_id": plan_id, "gate": result.get("evaluated"), "next": "PROPOSE"}))
