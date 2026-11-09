"""Demo step 2c: plan grants (T10 records) for one new plan: Job initial grant, workflow host, operator, gate subjects.

    python3 operations/demo-set/grants.py <plan-id> [--apply]

Runs the T10 calls inside the platform's t10-product-reader Pod (its own runtime DB identity) through the
operator's kubeconfig (cluster UID checked first). Without --apply it only reads the current grants.
Subjects and permissions come from the demo config (`grants`). Grants last 12 hours.
"""
import argparse
import json

import demo_config

p = argparse.ArgumentParser()
p.add_argument("plan_id")
p.add_argument("--apply", action="store_true")
a = p.parse_args()
cfg = demo_config.load()
demo_config.check(cfg, "platform")
inner=r'''
import json,time
from datetime import datetime,timezone,timedelta
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.window_server import runtime_connection
from bravo_platform_events.product_access import ProductInputAccess
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
D=json.loads(PACKET)
cfg=json.load(open('/product-read/reader.json')); tenant=cfg['tenant_id']
store=PostgresPlanStore(runtime_connection()); clock=lambda:datetime.now(timezone.utc).isoformat()
plan=D['plan_id']; job=D['job_principal']
pia=ProductInputAccess(store,clock=clock,purpose='t38-initial-proposal'); oa=OperatorAccess(store,clock=clock)
with store._transaction() as c:
    row=c.execute('SELECT state,version,tenant_id FROM plans WHERE plan_id=?',(plan,)).fetchone()
    jg=pia._current(c,plan,job); hg=OperatorAccess._grant(c,plan,'workflow-host')
out={'status':'PASS','plan':dict(row) if row else None,'config_tenant':tenant,'job_grant_rev':(jg or {}).get('revision',0),'job_grant_enabled':(jg or {}).get('enabled'),
     'host_grant_rev':(hg or {}).get('revision',0),'host_perms':(hg or {}).get('permissions')}
if D['apply']:
    now=datetime.now(timezone.utc); va=now.isoformat(); ex=(now+timedelta(hours=12)).isoformat()
    g=pia.set_grant(plan,job,tenant_id=row['tenant_id'],enabled=True,valid_after=va,expires_at=ex,expected_revision=out['job_grant_rev'],
                    administration_ref='user-demo-gate-plan-1108',command_key='job-initial-grant-1108')
    t=int(time.time()); hp=OperatorPrincipal('workflow-host',row['tenant_id'],t,t+300,t,'grant-1108')
    h=oa.set_permissions(plan,hp,permissions=['read','apply_change','gate:bind','gate:read','gate:evaluate','gate:consume','gate:record'],valid_after=va,expires_at=ex,expected_revision=out['host_grant_rev'],
                         administration_ref='user-demo-gate-plan-1108',command_key='workflow-host-grant-1108')
    with store._transaction() as c2: og=OperatorAccess._grant(c2,plan,D['operator'])
    op=OperatorPrincipal(D['operator'],row['tenant_id'],t,t+300,t,'grant-1108')
    o=oa.set_permissions(plan,op,permissions=D['operator_permissions'],valid_after=va,expires_at=ex,expected_revision=(og or {}).get('revision',0),
                         administration_ref='user-demo-gate-plan-1108',command_key='operator-grant-1108')
    out['operator_grant']={'revision':o['revision'],'permissions':o['permissions'],'expires_at':o['expires_at']}
    out['gate_grants']={}
    for subject,perms in D['gate_subjects'].items():
        with store._transaction() as c3: g0=OperatorAccess._grant(c3,plan,subject)
        sp=OperatorPrincipal(subject,row['tenant_id'],t,t+300,t,'grant-1108')
        r3=oa.set_permissions(plan,sp,permissions=perms,valid_after=va,expires_at=ex,expected_revision=(g0 or {}).get('revision',0),
                              administration_ref='user-demo-gate-plan-1108',command_key='gate-grant-'+subject+'-1108')
        out['gate_grants'][subject]=r3['permissions']
    out.update(applied=True,job_grant={'revision':g['revision'],'expires_at':g['expires_at']},host_grant={'revision':h['revision'],'permissions':h['permissions'],'expires_at':h['expires_at']})
print(json.dumps(out))
'''
packet = dict(cfg["grants"], plan_id=a.plan_id, apply=a.apply)
out = demo_config.kubectl(cfg, "platform", "-n", "platform-state", "exec", "-i", "deploy/t10-product-reader", "--",
                          "python", "-", input=inner.replace("PACKET", repr(json.dumps(packet))), timeout=120)
print(out.strip().splitlines()[-1])
