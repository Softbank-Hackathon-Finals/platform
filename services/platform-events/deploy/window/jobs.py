"""Bounded migration / synthetic enrollment Jobs; never exposed through HTTP."""

import copy
import hashlib
import json
from pathlib import Path

from render import NS, render

MIGRATE = '''import json, psycopg
from pathlib import Path
from bravo_platform_events.window_server import runtime_connection
try:
 with psycopg.connect(runtime_connection(), autocommit=True) as c:
  assert c.execute("SELECT current_user").fetchone()==('platform_migration',)
  c.execute("SET lock_timeout = '5s'")
  c.execute("SET statement_timeout = '60s'")
  c.execute(Path('/job/migration.sql').read_text(), prepare=False)
  expected=json.loads(Path('/job/expected.json').read_text())['schema_version']
  assert c.execute('SELECT version FROM bravo_platform.schema_version').fetchall()==[(expected,)]
 print(json.dumps({'schema_version':expected,'migration_committed':True}))
except Exception:
 print(json.dumps({'migration_committed':'UNKNOWN','error':'migration_unavailable'}));raise SystemExit(1) from None
'''

ENROLL = '''import json
from dataclasses import asdict
from datetime import UTC,datetime
from pathlib import Path
from bravo_platform_events.plans import PlanSpec
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.window_server import runtime_connection
from bravo_platform_events.window_store import ProducerIdentity,WindowIntake
try:
 identity=ProducerIdentity(**json.loads(Path('/producer/producer.json').read_text())['identity'])
 assert identity.plan_id.startswith('t10-ingress-probe-') and identity.tenant_id=='tenant-t10-ingress-probe'
 store=PostgresPlanStore(runtime_connection())
 now=datetime.now(UTC).isoformat()
 store.register_plan(PlanSpec(identity.plan_id,identity.tenant_id,'workload-probe','source-probe','target-probe','a'*64,'b'*64),run_id='t10-ingress-probe',occurred_at=now)
 operation='t10-ingress-enroll'
 receipt=WindowIntake(store).set_binding(identity,enabled=True,expected_revision=0,
  administration_ref='operator-t10-ingress-probe',command_key=operation)
 print(json.dumps({'synthetic_plan_registered':True,'binding_revision':receipt['revision']}))
except Exception:
 print(json.dumps({'enrollment':'UNKNOWN','error':'enrollment_unavailable'}));raise SystemExit(1) from None
'''


def job(image, phase, *, schema_version=2):
    if phase not in ("migrate", "enroll"):
        raise ValueError("unknown_phase")
    if type(schema_version) is not int or schema_version not in (2, 3):
        raise ValueError("unknown_storage_schema")
    if phase == "enroll" and schema_version != 2:
        raise ValueError("synthetic_enrollment_is_not_registration")
    data = {"run.py": MIGRATE if phase == "migrate" else ENROLL}
    if phase == "migrate":
        filename = {2: "0002_sli_windows.sql", 3: "0003_registrations.sql"}[schema_version]
        data["migration.sql"] = (Path(__file__).resolve().parents[2] / "sql" / filename).read_text()
        data["expected.json"] = '{"schema_version":' + str(schema_version) + '}'
    # The immutable ConfigMap name covers the SQL as well as its runner.
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    name = f"t10-window-{phase}-{digest}-{image[-8:]}"
    template = copy.deepcopy(render(image)["items"][1]["spec"]["template"])
    # It has no Service or ingress identity, and no API token or cluster role.
    template["metadata"]["labels"] = {"component": "platform", "bravo.dev/db-client": "runtime",
                                       "bravo.dev/window-job": phase}
    spec = template["spec"]
    spec["restartPolicy"] = "Never"
    container = spec["containers"][0]
    for key in ("ports", "readinessProbe", "livenessProbe"):
        container.pop(key)
    container["command"] = ["python", "/job/run.py"]
    container["volumeMounts"].append({"name": "job", "mountPath": "/job", "readOnly": True})
    spec["volumes"].append({"name": "job", "configMap": {"name": name}})
    if phase == "migrate":
        spec["volumes"][0]["secret"]["secretName"] = "platform-migration-db"
        spec["volumes"] = [v for v in spec["volumes"] if v["name"] != "producer"]
        container["volumeMounts"] = [v for v in container["volumeMounts"] if v["name"] != "producer"]
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"namespace": NS, "name": name},
         "immutable": True, "data": data},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": NS, "name": name},
         "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 120, "template": template}}]}, name
