"""Demo step 2a: open the T16 reader's foreground SSM tunnel (personal SSO). Keep it running during the scan."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import inventory_reader as r

p=argparse.ArgumentParser();p.add_argument('--profile',required=True);p.add_argument('--port',type=int,default=18516);a=p.parse_args()
import demo_config

c=json.loads(Path(demo_config.load()['t16']['reader_config']).read_text());env=r.environment();env['AWS_CONFIG_FILE']=str(Path.home()/'.aws/config')
who=r.aws(c,env,'sts','get-caller-identity',{},a.profile)
r.require(who['Account']==c['account'] and who['UserId'] in c['allowed_sso_user_ids'],'personal_sso_identity_mismatch')
with r.socket.socket() as s:r.require(s.connect_ex(('127.0.0.1',a.port))!=0,'local_port_busy_do_not_reuse')
params={'host':[urlsplit(c['endpoint']).hostname],'portNumber':['443'],'localPortNumber':[str(a.port)]}
print('Use this foreground session only. Keep its SessionId for explicit termination.',flush=True)
argv=['aws','--profile',a.profile,'--region',c['region'],'ssm','start-session','--target',c['ssm_target'],'--document-name','AWS-StartPortForwardingSessionToRemoteHost','--parameters',json.dumps(params)]
os.execvpe('aws',argv,env)
