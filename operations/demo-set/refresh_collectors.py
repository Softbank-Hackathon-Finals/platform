"""Demo step 0: fresh registration collector tokens (source and target), as a new immutable Secret.

    python3 operations/demo-set/refresh_collectors.py <new-secret-name> [--hours 8]

Mints `bravo-collector` tokens on both clusters through the operator's kubeconfigs (UIDs checked first),
copies the CA keys of the current Secret and creates <new-secret-name>. Token values stay in memory.
Then point `registration.runtime.secrets.collectors` at the new name in the bravo-gitops release.
"""

import argparse
import base64
import json

import demo_config

p = argparse.ArgumentParser()
p.add_argument("name")
p.add_argument("--hours", type=int, default=8)
a = p.parse_args()
cfg = demo_config.load()
for side in demo_config.SIDES:
    demo_config.check(cfg, side)
current = cfg["registration"]["collectors_secret"]
tokens = {f"{side}-token": demo_config.kubectl(cfg, side, "-n", cfg["collector_namespaces"][side], "create", "token",
                                               "bravo-collector", f"--duration={a.hours}h").strip()
          for side in ("source", "target")}
prev = json.loads(demo_config.kubectl(cfg, "platform", "-n", "platform-state", "get", "secret", current, "-o", "json"))
data = {k: v for k, v in prev["data"].items() if k not in tokens}
data |= {k: base64.b64encode(v.encode()).decode() for k, v in tokens.items()}
del tokens
secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True, "data": data,
          "metadata": {"name": a.name, "namespace": "platform-state", "labels": prev["metadata"].get("labels", {})}}
print(demo_config.kubectl(cfg, "platform", "create", "-f", "-", input=json.dumps(secret)).strip())
print(json.dumps({"status": "PASS", "secret": a.name, "values": {"registration": {"runtime": {"secrets": {
    "collectors": a.name}}}}}))
