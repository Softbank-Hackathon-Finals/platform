"""Demo step 1: register one plan through the registration API with the operator's own SSO proof.

    uv run --directory services/platform-events python ../../operations/demo-set/register_plan.py <idempotency-key>

Reads the workload id, profiles, target environment and the registration endpoint/CA from the demo config.
Same key -> same plan (idempotent). Writes <workdir>/<plan-id>/new-plan-receipt.json and prints the plan id.
"""

import json
import ssl
import sys

import demo_config
import httpx
from bravo_platform_events.registration_sso import make_registration_token

cfg = demo_config.load()
reg, plan = cfg["registration"], cfg["plan"]
key = sys.argv[1]
tls = ssl.create_default_context(cafile=reg["ca_file"])
tls.check_hostname = reg.get("check_hostname", True)  # the current certificate names 127.0.0.1 only
body = {"workload_id": plan["workload_id"], "target_environment_id": plan["target_environment_id"],
        "profile_id": plan["profile"].split("/")[0], "profile_version": plan["profile"].split("/")[1],
        "recheck_profile_id": plan["recheck_profile"].split("/")[0],
        "recheck_profile_version": plan["recheck_profile"].split("/")[1]}
token = make_registration_token(profile_name=cfg["aws_profile"], region=cfg["aws_region"],
                                audience="bravo-platform-registration")
r = httpx.post(reg["url"] + "/v1/plans", json=body, timeout=60, verify=tls,
               headers={"Authorization": "Bearer " + token, "Idempotency-Key": key})
del token
if r.status_code not in (200, 201):
    raise SystemExit(json.dumps({"status": "FAIL", "http": r.status_code, "body": r.text[:400]}))
created = r.json()
(demo_config.plan_dir(cfg, created["plan_id"]) / "new-plan-receipt.json").write_text(r.text)
print(json.dumps({"status": "PASS", "plan_id": created["plan_id"], "state": created.get("state"),
                  "version": created.get("version")}))
