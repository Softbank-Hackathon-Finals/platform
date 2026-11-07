"""Public entry reconciler: Git-declared hostnames -> one Cloudflare Tunnel route, DNS and Access.

The platform holds the Cloudflare credential (OpenBao edge/cloudflare-gitops via ESO). Clusters only run a
connector; the customer side is reached over the tailnet, so no customer cluster needs a Cloudflare secret.

Spec (ConfigMap rendered from infra/envs/public-entry/entries.json):
  {"zone": "...", "tunnel": "...", "access": {"app": "...", "policy": "...", "session_duration": "8h"},
   "entries": [{"hostname": "<label>.<zone>", "service": "http://<svc>.<ns>.svc:<port>", "public": false}]}
"public": true leaves that host without Access (no login, no origin Access check); default is Access.
"deny_paths": ["^/regex", ...] answers 403 at the edge for those paths of the host, before its route.
Credential files: token, account_id, zone_id, access_emails (comma separated; never in Git).

Only objects this reconciler owns change: the named tunnel, the named Access app and policy, and CNAMEs whose
comment is MARK. A record without MARK on a declared hostname stops the run before any write to DNS.
Protected entries sit behind Access, and their tunnel route also requires the Access token at the origin.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MARK = "managed-by: bravo-public-entry"
API = "https://api.cloudflare.com/client/v4"
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
SERVICE = re.compile(r"https?://[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.svc(?:\.cluster\.local)?:\d{1,5}\Z")
EMAIL = re.compile(r"[^@\s,]+@[a-z0-9.-]+\.[a-z]{2,}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ()._-]{0,63}\Z")


class Refused(Exception):
    """The declared state cannot be applied safely; nothing further is written in this run."""


def validate(spec: dict) -> dict:
    zone = spec["zone"]
    if not all(LABEL.match(part) for part in zone.split(".")) or zone.count(".") < 1:
        raise Refused("zone")
    for key in (spec["tunnel"], spec["access"]["app"], spec["access"]["policy"]):
        if not NAME.match(key):
            raise Refused("name")
    seen = set()
    for entry in spec["entries"]:
        host, service = entry["hostname"], entry["service"]
        # One label under the zone: Cloudflare's universal certificate covers *.<zone> only.
        label = host.removesuffix("." + zone)
        if label == host or not LABEL.match(label) or host in seen:
            raise Refused("hostname:" + host)
        if not SERVICE.match(service):
            raise Refused("service:" + host)
        if not isinstance(entry.get("public", False), bool):
            raise Refused("public:" + host)
        deny = entry.get("deny_paths", [])
        if not (isinstance(deny, list) and all(isinstance(d, str) and d.startswith("^") for d in deny)):
            raise Refused("deny_paths:" + host)
        for d in deny:
            re.compile(d)
        seen.add(host)
    if not seen:
        raise Refused("no entries")
    return spec


def emails_from(text: str) -> list[str]:
    emails = sorted({e.strip().lower() for e in text.split(",") if e.strip()})
    if not emails or not all(EMAIL.match(e) for e in emails):
        raise Refused("access_emails")
    return emails


class Cloudflare:
    def __init__(self, token: str, opener=urllib.request.urlopen):
        self._token, self._open = token, opener

    def call(self, method: str, path: str, body=None):
        req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=30) as res:
                doc = json.load(res)
        except urllib.error.HTTPError as e:
            doc = json.load(e) if e.fp else {"success": False, "errors": [{"code": e.code}]}
        if not doc.get("success"):
            raise RuntimeError(f"{method} {path.split('?')[0]}: {[x.get('code') for x in doc.get('errors', [])]}")
        return doc["result"]

    def list(self, path: str) -> list:
        out, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            result = self.call("GET", f"{path}{sep}per_page=50&page={page}")
            out += result
            if len(result) < 50:
                return out
            page += 1


def _ensure(cf, path, found, body, same):
    if found is None:
        return cf.call("POST", path, body)
    if same(found):
        return found
    return cf.call("PUT", f"{path}/{found['id']}", body)


def reconcile(cf: Cloudflare, spec: dict, account: str, zone: str, emails: list[str], write_token) -> dict:
    hosts = [e["hostname"] for e in spec["entries"]]
    acct = f"/accounts/{account}"
    changed = []

    tunnels = cf.list(f"{acct}/cfd_tunnel?is_deleted=false&name={spec['tunnel']}")
    tunnel = tunnels[0] if tunnels else cf.call("POST", f"{acct}/cfd_tunnel", {"name": spec["tunnel"], "config_src": "cloudflare"})
    if not tunnels:
        changed.append("tunnel")

    protected = [e["hostname"] for e in spec["entries"] if not e.get("public")]
    policies = [p for p in cf.list(f"{acct}/access/policies") if p["name"] == spec["access"]["policy"]]
    apps = [a for a in cf.list(f"{acct}/access/apps") if a["name"] == spec["access"]["app"]]
    origin = {}
    if protected:
        include = [{"email": {"email": e}} for e in emails]
        policy_body = {"name": spec["access"]["policy"], "decision": "allow", "include": include}
        policy = _ensure(cf, f"{acct}/access/policies", policies[0] if policies else None, policy_body,
                         lambda p: p["decision"] == "allow" and sorted(i["email"]["email"] for i in p["include"] if "email" in i) == emails
                         and len(p["include"]) == len(emails))
        if not policies or policy is not policies[0]:
            changed.append("access_policy")
        app_body = {"name": spec["access"]["app"], "type": "self_hosted", "domain": protected[0],
                    "destinations": [{"type": "public", "uri": h} for h in protected],
                    "session_duration": spec["access"].get("session_duration", "8h"), "app_launcher_visible": True,
                    "policies": [{"id": policy["id"], "precedence": 1}]}
        app = _ensure(cf, f"{acct}/access/apps", apps[0] if apps else None, app_body,
                      lambda a: sorted(d.get("uri") for d in a.get("destinations", [])) == sorted(protected)
                      and [p["id"] for p in a.get("policies", [])] == [policy["id"]])
        if not apps or app is not apps[0]:
            changed.append("access_app")
        team = cf.call("GET", f"{acct}/access/organizations")["auth_domain"].split(".")[0]
        origin = {"originRequest": {"access": {"required": True, "teamName": team, "audTag": [app["aud"]]}}}
    else:
        # Every entry is public: the owned Access app and policy protect nothing. Remove them, app first.
        for a in apps:
            cf.call("DELETE", f"{acct}/access/apps/{a['id']}")
            changed.append("access_app_removed")
        for p in policies:
            cf.call("DELETE", f"{acct}/access/policies/{p['id']}")
            changed.append("access_policy_removed")

    # A public entry has no Access in front and no Access check at the origin.
    # deny_paths answer 403 at the edge before the host's route (cloudflared matches rules in order, path is RE2).
    ingress = [r for e in spec["entries"] for r in
               [{"hostname": e["hostname"], "path": d, "service": "http_status:403"} for d in e.get("deny_paths", [])]
               + [{"hostname": e["hostname"], "service": e["service"], **({} if e.get("public") else origin)}]] \
        + [{"service": "http_status:404"}]
    current = (cf.call("GET", f"{acct}/cfd_tunnel/{tunnel['id']}/configurations").get("config") or {}).get("ingress")
    if _route(current) != _route(ingress):
        cf.call("PUT", f"{acct}/cfd_tunnel/{tunnel['id']}/configurations", {"config": {"ingress": ingress}})
        changed.append("tunnel_route")

    target = f"{tunnel['id']}.cfargotunnel.com"
    records = cf.list(f"/zones/{zone}/dns_records?type=CNAME")
    by_name = {r["name"]: r for r in records}
    foreign = [h for h in hosts if h in by_name and by_name[h].get("comment") != MARK]
    if foreign:
        raise Refused("foreign DNS record: " + ",".join(foreign))
    for h in hosts:
        body = {"type": "CNAME", "name": h, "content": target, "proxied": True, "comment": MARK}
        rec = by_name.get(h)
        if rec is None:
            cf.call("POST", f"/zones/{zone}/dns_records", body)
            changed.append("dns:" + h)
        elif rec["content"] != target or not rec["proxied"]:
            cf.call("PUT", f"/zones/{zone}/dns_records/{rec['id']}", body)
            changed.append("dns:" + h)
    for rec in records:
        if rec.get("comment") == MARK and rec["name"] not in hosts:
            cf.call("DELETE", f"/zones/{zone}/dns_records/{rec['id']}")
            changed.append("dns-removed:" + rec["name"])

    if write_token(cf.call("GET", f"{acct}/cfd_tunnel/{tunnel['id']}/token")):
        changed.append("tunnel_token")
    return {"tunnel": spec["tunnel"], "hosts": hosts, "changed": changed}


def _route(ingress):
    if not ingress:
        return None
    return [(r.get("hostname"), r.get("path"), r.get("service"), json.dumps((r.get("originRequest") or {}).get("access"), sort_keys=True))
            for r in ingress]


def file_writer(path: Path):
    """The connector in the same Pod reads the token from a memory volume; it never becomes a Secret.
    Returns True when the file changed (a recreated tunnel); the connector's probe then restarts it."""
    def write(value: str) -> bool:
        if path.exists() and path.read_text() == value:
            return False
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value)
        tmp.chmod(0o440)
        tmp.replace(path)
        return True

    return write


def main():
    spec_file = Path(os.environ.get("ENTRY_SPEC", "/etc/public-entry/entries.json"))
    creds = Path(os.environ.get("CLOUDFLARE_CREDENTIALS", "/run/cloudflare"))
    write = file_writer(Path(os.environ.get("TUNNEL_TOKEN_FILE", "/run/tunnel/token")))
    interval = int(os.environ.get("RECONCILE_SECONDS", "60"))
    while True:
        try:
            # The ConfigMap and the ESO Secret change in place; read both every round instead of restarting.
            spec = validate(json.loads(spec_file.read_text()))
            cf = Cloudflare((creds / "token").read_text().strip())
            result = reconcile(cf, spec, (creds / "account_id").read_text().strip(), (creds / "zone_id").read_text().strip(),
                               emails_from((creds / "access_emails").read_text()), write)
            print(json.dumps({"ok": True, **result}), flush=True)
        except (Refused, RuntimeError, OSError, ValueError, KeyError) as e:  # retry next round; never print credentials
            print(json.dumps({"ok": False, "error": type(e).__name__, "detail": str(e)[:300]}), file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
