"""S1 -> S2 prep: publish the plan's signed TaskCatalog entry (credential-migration) with a one-off Job.

Usage (from this directory): python3 catalog_task.py <plan-id>
Uses the live t10-registration image and config, the catalog PVC and key Secret (same as the Reader).
Idempotent (existing entry is read back). Writes ../<plan>/catalog-job.json and prints the Job's one JSON line.
"""
import json
import subprocess
import sys
from pathlib import Path

import demo_config

CFG = demo_config.load()
RUNS = CFG["workdir"]  # <workdir>/<plan-id>/...
plan_id = sys.argv[1]
app = json.loads(Path(CFG["workload_receipt"]).read_text())
kc = demo_config.kubeconfig(CFG, "platform")
K = ["kubectl", "--kubeconfig", kc, "-n", "platform-state"]
assert subprocess.run(["kubectl", "--kubeconfig", kc, "get", "ns", "kube-system", "-o", "jsonpath={.metadata.uid}"],
                      capture_output=True, text=True).stdout == "5d978ed5-52a9-405a-a4ce-fd2e1c3ac9f9", "platform_identity"
dep = json.loads(subprocess.run(K + ["get", "deploy", "t10-registration", "-o", "json"], capture_output=True, text=True, check=True).stdout)
img = dep["spec"]["template"]["spec"]["containers"][0]["image"]
regcm = [v["configMap"]["name"] for v in dep["spec"]["template"]["spec"]["volumes"] if v.get("name") == "registration"][0]
name = "t10-catalog-publish-" + plan_id[5:13]
import json
import sys

script=r'''
import hashlib,json
from pathlib import Path
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.window_server import runtime_connection
from rehearsal_runner.task_catalog import TaskCatalog,CatalogUnavailable
from rehearsal_runner.admission import logical_task_digest
PID=PLAN_ID_VALUE;SOURCE_SHA=SOURCE_SHA_VALUE;NAME='credential-migration'
try:
 store=PostgresPlanStore(runtime_connection());root=Path('/catalog');catalog=TaskCatalog(root,Path('/catalog-key/key').read_bytes())
 before={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
 with store._transaction(write=True,plan_id=PID) as c:
  row=dict(c.execute('SELECT * FROM plans WHERE plan_id=?',(PID,)).fetchone());spec=json.loads(row['spec_json'])
  assert row['tenant_id']=='bravo' and spec['profile_id'] in ('full','demo-gate') and spec['workload_id']==WORKLOAD_VALUE
  try: task=catalog.for_plan(c,row,NAME); created=False
  except CatalogUnavailable:
   catalog.register(plan_id=PID,tenant_id='bravo',plan_digest=spec['plan_digest'],task_id=NAME,kind=NAME,source_sha=SOURCE_SHA); created=True
   task=catalog.for_plan(c,row,NAME)
  assert task.source_sha==SOURCE_SHA
  neg={}
  for label,cand,rd in [('wrong_tenant',row|{'tenant_id':'other'},catalog),('wrong_digest',row|{'spec_json':json.dumps(spec|{'plan_digest':'f'*64})},catalog),('wrong_key',row,TaskCatalog(root,b'x'*32))]:
   try: rd.for_plan(c,cand,NAME)
   except CatalogUnavailable: neg[label]=True
 after={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
 assert all(after.get(k)==v for k,v in before.items()) and len(after)==len(before)+(1 if created else 0)
 print(json.dumps({'status':'PASS','plan_id':PID,'created':created,'task_digest':logical_task_digest(task),'negatives':neg,'old_files_preserved':True}))
except Exception as e:
 print(json.dumps({'status':'FAIL','error':type(e).__name__,'msg':str(e)[:200]}));raise SystemExit(1)
'''
script = (script.replace('PLAN_ID_VALUE', repr(plan_id)).replace('SOURCE_SHA_VALUE', repr(app['scope']['source_sha']))
          .replace('WORKLOAD_VALUE', repr(app['workload_id'])))
lab={'app.kubernetes.io/name':'t10-catalog-publisher','bravo.dev/db-client':'runtime','component':'platform'}
cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':'platform-state','labels':lab},'immutable':True,'data':{'publish.py':script}}
job={'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':'platform-state','labels':lab},
 'spec':{'backoffLimit':0,'activeDeadlineSeconds':120,'template':{'metadata':{'labels':lab},'spec':{'serviceAccountName':'t10-registration','automountServiceAccountToken':False,'restartPolicy':'Never',
  'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},
  'containers':[{'name':'publish','image':img,'imagePullPolicy':'IfNotPresent','command':['python','/script/publish.py'],
   'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
   'resources':{'requests':{'cpu':'100m','memory':'192Mi'},'limits':{'cpu':'1','memory':'512Mi'}},'env':[{'name':'PYTHONDONTWRITEBYTECODE','value':'1'}],
   'volumeMounts':[{'name':'credential','mountPath':'/credential','readOnly':True},{'name':'ca','mountPath':'/ca','readOnly':True},{'name':'registration','mountPath':'/registration','readOnly':True},{'name':'script','mountPath':'/script','readOnly':True},{'name':'catalog','mountPath':'/catalog'},{'name':'catalog-key','mountPath':'/catalog-key','readOnly':True}]}],
  'volumes':[{'name':'credential','secret':{'secretName':'platform-runtime-db','defaultMode':288,'items':[{'key':'username','path':'username'},{'key':'password','path':'password'}]}},{'name':'ca','secret':{'secretName':'platform-db-ca','defaultMode':288,'items':[{'key':'ca.crt','path':'ca.crt'}]}},{'name':'registration','configMap':{'name':regcm,'defaultMode':292}},{'name':'script','configMap':{'name':name}},{'name':'catalog','persistentVolumeClaim':{'claimName':CFG['catalog']['claim']}},{'name':'catalog-key','secret':{'secretName':CFG['catalog']['key_secret'],'defaultMode':288,'items':[{'key':'key','path':'key'}]}}],
  'imagePullSecrets':[{'name':'platform-ghcr-reader'}]}}}}
manifest={'apiVersion':'v1','kind':'List','items':[cm,job]}
(RUNS / plan_id / "catalog-job.json").write_text(json.dumps(manifest))
subprocess.run(K + ["create", "-f", "-"], input=json.dumps(manifest), text=True, capture_output=True)
subprocess.run(K + ["wait", "--for=condition=complete", "job/" + name, "--timeout=150s"], capture_output=True)
print(subprocess.run(K + ["logs", "job/" + name], capture_output=True, text=True).stdout.strip().splitlines()[-1:])
