import copy
import re

import pytest

from bravo_public_entry.reconcile import MARK, Refused, emails_from, reconcile, validate

SPEC = {
    "zone": "example.ai",
    "tunnel": "bravo-platform-entry",
    "access": {"app": "Bravo public entry", "policy": "Team Bravo members", "session_duration": "8h"},
    "token_secret": {"namespace": "bravo-gitops", "name": "argocd-tunnel-token", "key": "token"},
    "entries": [
        {"hostname": "platform-argocd.example.ai", "service": "http://bravo-gitops-server.bravo-gitops.svc:80"},
        {"hostname": "demo-chat-01-argocd.example.ai", "service": "http://bravo-target-argocd.bravo-gitops.svc:80"},
    ],
}
EMAILS = ["a@example.com", "b@example.org"]


class FakeCloudflare:
    """In-memory Cloudflare v4 subset; records every mutating call."""

    def __init__(self):
        self.tunnels, self.policies, self.apps, self.records, self.configs = [], [], [], [], {}
        self.writes, self._n = [], 0

    def _id(self):
        self._n += 1
        return f"id{self._n}"

    def list(self, path):
        return self.call("GET", path)

    def call(self, method, path, body=None):
        path = path.split("&per_page")[0].split("?per_page")[0]
        if method != "GET":
            self.writes.append((method, re.sub(r"id\d+", "<id>", path)))
        if m := re.fullmatch(r"/accounts/acct/cfd_tunnel\?is_deleted=false&name=(.+)", path):
            return [t for t in self.tunnels if t["name"] == m[1]]
        if path == "/accounts/acct/cfd_tunnel" and method == "POST":
            self.tunnels.append({"id": self._id(), "name": body["name"]})
            return self.tunnels[-1]
        if m := re.fullmatch(r"/accounts/acct/cfd_tunnel/(\w+)/configurations", path):
            if method == "PUT":
                self.configs[m[1]] = body["config"]
            return {"config": self.configs.get(m[1])}
        if m := re.fullmatch(r"/accounts/acct/cfd_tunnel/(\w+)/token", path):
            return "token-" + m[1]
        if path == "/accounts/acct/access/organizations":
            return {"auth_domain": "team.cloudflareaccess.com"}
        for kind, store in (("policies", self.policies), ("apps", self.apps)):
            if path == f"/accounts/acct/access/{kind}":
                if method == "GET":
                    return store
                store.append({**copy.deepcopy(body), "id": self._id(), "aud": "aud1"})
                return store[-1]
            if m := re.fullmatch(rf"/accounts/acct/access/{kind}/(\w+)", path):
                item = next(x for x in store if x["id"] == m[1])
                item.update(copy.deepcopy(body))
                return item
        if path == "/zones/zone/dns_records?type=CNAME":
            return self.records
        if path == "/zones/zone/dns_records":
            self.records.append({**body, "id": self._id()})
            return self.records[-1]
        if m := re.fullmatch(r"/zones/zone/dns_records/(\w+)", path):
            rec = next(r for r in self.records if r["id"] == m[1])
            if method == "DELETE":
                self.records.remove(rec)
            else:
                rec.update(body)
            return rec
        raise AssertionError(f"unexpected {method} {path}")


def run(cf, spec=SPEC, tokens=None):
    tokens = tokens if tokens is not None else []

    def write(value):
        if tokens and tokens[-1] == value:
            return False
        tokens.append(value)
        return True

    return reconcile(cf, validate(copy.deepcopy(spec)), "acct", "zone", EMAILS, write)


def test_first_run_creates_then_second_run_changes_nothing():
    cf, tokens = FakeCloudflare(), []
    first = run(cf, tokens=tokens)
    assert set(first["changed"]) >= {"tunnel", "access_policy", "access_app", "tunnel_route", "tunnel_token"}
    assert sorted(r["name"] for r in cf.records) == ["demo-chat-01-argocd.example.ai", "platform-argocd.example.ai"]
    assert all(r["comment"] == MARK and r["proxied"] and r["content"] == "id1.cfargotunnel.com" for r in cf.records)
    route = cf.configs["id1"]["ingress"]
    assert route[-1] == {"service": "http_status:404"}
    assert all(r["originRequest"]["access"] == {"required": True, "teamName": "team", "audTag": ["aud1"]} for r in route[:-1])
    assert tokens == ["token-id1"]

    cf.writes.clear()
    assert run(cf, tokens=tokens)["changed"] == []
    assert cf.writes == []


def test_removed_entry_drops_only_its_managed_record_and_destination():
    cf = FakeCloudflare()
    cf.records.append({"id": "other", "name": "blog.example.ai", "content": "x", "proxied": True, "comment": ""})
    run(cf)
    smaller = copy.deepcopy(SPEC)
    smaller["entries"] = smaller["entries"][:1]
    changed = run(cf, smaller)["changed"]
    assert "dns-removed:demo-chat-01-argocd.example.ai" in changed
    assert sorted(r["name"] for r in cf.records) == ["blog.example.ai", "platform-argocd.example.ai"]
    assert [d["uri"] for d in cf.apps[0]["destinations"]] == ["platform-argocd.example.ai"]


def test_foreign_record_on_declared_host_stops_before_dns_writes():
    cf = FakeCloudflare()
    cf.records.append({"id": "manual", "name": "platform-argocd.example.ai", "content": "x", "proxied": True, "comment": "hand"})
    with pytest.raises(Refused, match="foreign DNS record"):
        run(cf)
    assert not any(path.startswith("/zones") for _, path in cf.writes)
    assert cf.records == [{"id": "manual", "name": "platform-argocd.example.ai", "content": "x", "proxied": True, "comment": "hand"}]


def test_policy_follows_the_secret_email_list():
    cf = FakeCloudflare()
    run(cf)
    cf.writes.clear()
    reconcile(cf, validate(copy.deepcopy(SPEC)), "acct", "zone", ["a@example.com"], lambda v: False)
    assert ("PUT", "/accounts/acct/access/policies/<id>") in cf.writes
    assert [i["email"]["email"] for i in cf.policies[0]["include"]] == ["a@example.com"]


@pytest.mark.parametrize("entry", [
    {"hostname": "argocd.other.ai", "service": "http://a.b.svc:80"},           # outside the zone
    {"hostname": "a.b.example.ai", "service": "http://a.b.svc:80"},             # two labels: no universal cert
    {"hostname": "ok.example.ai", "service": "http://10.0.0.1:80"},             # not an in-cluster Service
    {"hostname": "ok.example.ai", "service": "http://a.b.svc.evil.com:80"},
])
def test_validate_refuses(entry):
    spec = copy.deepcopy(SPEC)
    spec["entries"] = [entry]
    with pytest.raises(Refused):
        validate(spec)


def test_emails_must_be_present_and_well_formed():
    assert emails_from(" B@x.org, a@y.com ,") == ["a@y.com", "b@x.org"]
    for bad in ("", "not-an-email", "a@b"):
        with pytest.raises(Refused):
            emails_from(bad)
