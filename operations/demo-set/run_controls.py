"""One T14 control run (operations/controls/run.py) for the source or target side of a plan.

    python3 operations/demo-set/run_controls.py <plan-dir> <target|source> <app> <environment-id>

The registration comes from the demo config (`t14.<side>`) with these placeholders filled here:
  {ca_file} {wrong_ca_file}       CA of the side's secret store / database (read from the cluster)
  {jwt:<sa>} {uid:<sa>}           a 10-minute ServiceAccount token file and the SA UID (target, OpenBao)
  {pooler_uid} {db_username}      the source pooler Deployment UID and the app's own DB user (source, ENC)
  {psql} {psql_sha256}            the local psql used by the pooler TLS probe
Secrets (object keys, DB password) go only to the runner's environment. Token files are deleted after the run.
Port-forwards listed in `t14.<side>.forwards` are opened for the run and closed after it.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import demo_config

cfg = demo_config.load()
plan_dir, side, app, environment_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
spec = cfg["t14"][side]
demo_config.check(cfg, side)
private = plan_dir / "private"
private.mkdir(mode=0o700, exist_ok=True)


def k(*args):
    return demo_config.kubectl(cfg, side, *args)


def write(name, data):
    path = private / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    return str(path)


values, run_env, temporary = {}, dict(os.environ, KUBECONFIG=demo_config.kubeconfig(cfg, side)), []
ns = spec["namespace"]
if side == "target":
    values["ca_file"] = write("target-ca.pem", k("-n", ns, "get", "cm", spec["ca_configmap"], "-o",
                                                 "jsonpath={.data.ca\\.crt}"))
    for sa in spec["service_accounts"]:
        values[f"uid:{sa}"] = k("-n", ns, "get", "sa", sa, "-o", "jsonpath={.metadata.uid}")
        values[f"jwt:{sa}"] = write(f"{sa}.jwt", k("-n", ns, "create", "token", sa, "--audience",
                                                   spec["token_audience"], "--duration", "10m"))
        temporary.append(values[f"jwt:{sa}"])
    objects = json.loads(k("-n", ns, "get", "secret", spec["object_secret"], "-o", "json"))["data"]
    run_env |= {"T14_OBJECT_ACCESS_KEY": base64.b64decode(objects["access_key"]).decode(),
                "T14_OBJECT_SECRET_KEY": base64.b64decode(objects["secret_key"]).decode()}
else:
    values["ca_file"] = write("source-db-ca.pem", base64.b64decode(k("-n", ns, "get", "secret", spec["db_ca_secret"],
                                                                     "-o", "jsonpath={.data.ca\\.crt}")).decode())
    values["wrong_ca_file"] = str(private / "target-ca.pem")
    values["pooler_uid"] = k("-n", ns, "get", "deploy", spec["pooler_deployment"], "-o", "jsonpath={.metadata.uid}")
    # The app's own DB credential, read as the app role, kept in memory only.
    cred = json.loads(subprocess.run(["aws", "sts", "assume-role", "--profile", cfg["aws_profile"], "--role-arn",
        spec["app_role_arn"], "--role-session-name", "bravo-t14-enc", "--duration-seconds", "900", "--output", "json"],
        capture_output=True, text=True, check=True, timeout=60).stdout)["Credentials"]
    aws = {k_: v for k_, v in os.environ.items() if k_ != "AWS_PROFILE"} | {
        "AWS_ACCESS_KEY_ID": cred["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": cred["SecretAccessKey"],
        "AWS_SESSION_TOKEN": cred["SessionToken"], "AWS_REGION": cfg["aws_region"]}
    db = json.loads(json.loads(subprocess.run(["aws", "secretsmanager", "get-secret-value", "--secret-id",
        spec["db_secret_id"], "--output", "json"], capture_output=True, text=True, env=aws, check=True,
        timeout=60).stdout)["SecretString"])
    values["db_username"] = db.get("username") or db.get("user")
    run_env["T14_POOLER_PASSWORD"] = db["password"]
    psql = os.path.realpath(spec["psql"])
    values["psql"], values["psql_sha256"] = psql, "sha256:" + hashlib.sha256(Path(psql).read_bytes()).hexdigest()


def fill(value):
    if isinstance(value, dict):
        return {key: fill(v) for key, v in value.items()}
    if isinstance(value, str):
        whole = re.fullmatch(r"\{([\w:.-]+)\}", value)
        if whole:
            return values[whole.group(1)]
    return value


registration = {"app": app, "environment": {"id": environment_id, "role": side, "ref": spec["environment_ref"]},
                "profile": {"id": spec["profile"].split("/")[0], "version": spec["profile"].split("/")[1]},
                "tests": fill(spec["tests"])}
reg = write(f"registration-{side}.json", json.dumps(registration, indent=1))
forwards = [subprocess.Popen(["kubectl", "--kubeconfig", demo_config.kubeconfig(cfg, side), "-n", f["namespace"],
                              "port-forward", f["service"], f["ports"]], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL) for f in spec.get("forwards", [])]
try:
    time.sleep(6 if forwards else 0)
    out = plan_dir / f"{side}-{time.strftime('%Y%m%dT%H%M%S')}"
    r = subprocess.run(["uv", "run", "--quiet", "--directory", "services/control-runner", "python",
                        "../../operations/controls/run.py", "--registration", reg, "--out", str(out)],
                       cwd=cfg["repo"], env=run_env, capture_output=True, text=True, timeout=600)
finally:
    for p in forwards:
        p.terminate()
    for path in temporary:
        os.unlink(path)
print(r.stdout[-1500:])
print(r.stderr[-800:], file=sys.stderr)
run = json.loads((out / "control-run.json").read_text())
print(json.dumps({"side": side, "dir": str(out), "run_id": run["run_id"],
                  "verdicts": {x["test_id"]: x["verdict"] for x in run["results"]}}))
