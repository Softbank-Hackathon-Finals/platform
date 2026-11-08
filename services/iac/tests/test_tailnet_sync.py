"""T44 Tailscale GitOps: add-only merge of the fragment, and plan/apply against a fake Tailscale API."""

import copy
import json
from pathlib import Path

import pytest

from bravo_iac import IacError, tailnet_sync
from bravo_iac.cli import main
from bravo_iac.tailnet import check_policy, fragment
from bravo_iac.tailnet_sync import Client, github_token, masked, merge, prune, sync

REPO = Path(__file__).resolve().parents[3]
ENVIRONMENTS = sorted((REPO / "infra/iac/environments").glob("*.json"))
STATES = [json.loads(p.read_text()) for p in ENVIRONMENTS]
WANTED = fragment(STATES)
# What Git must never hold: people in groups, SSH rules, a grant someone added by hand.
PEOPLE = {
    "groups": {"group:bravo-operators": ["someone@example.com"]},
    "ssh": [{"action": "accept", "src": ["group:bravo-operators"], "dst": ["autogroup:self"], "users": ["root"]}],
    "grants": [{"src": ["group:bravo-operators"], "dst": ["tag:bravo-platform-operator"], "ip": ["tcp:443"]}],
}


class FakeTailscale:
    """The policy file endpoints: ETag on read, If-Match on write, validate as configured."""

    def __init__(self, policy, validate=(200, {}), lose_write=False, comments=False):
        self.policy, self.version, self.calls = copy.deepcopy(policy), 1, []
        self.validate, self.lose_write, self.comments = validate, lose_write, comments

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.rsplit("/", 1)[-1], headers.get("If-Match")))
        assert headers["Authorization"] == "Bearer short-lived"
        assert "/tailnet/example.org/acl" in url
        etag = f'"v{self.version}"'
        if method == "GET":
            text = json.dumps(self.policy)
            if headers["Accept"] == "application/hujson" and self.comments:
                text = "// keep me\n" + text
            return 200, {"ETag": etag}, text.encode()
        if url.endswith("/validate"):
            return self.validate[0], {}, json.dumps(self.validate[1]).encode()
        if headers.get("If-Match") != etag:
            return 412, {}, b"{}"
        self.policy, self.version = json.loads(body), self.version + 1
        if self.lose_write:
            raise TimeoutError
        return 200, {"ETag": f'"v{self.version}"'}, body


def client(fake):
    return Client("example.org", "short-lived", send=fake)


def test_merge_adds_the_fragment_and_keeps_everything_else():
    merged, added, conflicts = merge(PEOPLE, WANTED)
    assert conflicts == [] and check_policy(merged, STATES) == []
    assert merged["groups"] == PEOPLE["groups"] and merged["ssh"] == PEOPLE["ssh"]
    assert merged["grants"][0] == PEOPLE["grants"][0]  # the hand-written grant stays first, unchanged
    assert "GRANT:tag:bravo-cutover-control->tag:bravo-target-db:tcp:5432" in added
    assert PEOPLE["grants"] == [{"src": ["group:bravo-operators"], "dst": ["tag:bravo-platform-operator"],
                                 "ip": ["tcp:443"]}]  # the input is not modified
    again, more, _ = merge(merged, WANTED)
    assert more == [] and again == merged  # applying twice changes nothing


def test_merge_never_removes_and_never_overwrites_a_tag_owner():
    merged, _, _ = merge(PEOPLE, WANTED)
    dropped = copy.deepcopy(WANTED)
    dropped["grants"].pop()  # a route removed from Git
    assert merge(merged, dropped)[0] == merged  # the grant stays for a person to delete
    tag = next(iter(WANTED["tagOwners"]))
    other = {**merged, "tagOwners": {**merged["tagOwners"], tag: ["autogroup:member"]}}
    result, added, conflicts = merge(other, WANTED)
    assert conflicts == [f"TAG_OWNER:{tag}"] and result["tagOwners"][tag] == ["autogroup:member"]


def test_a_covering_grant_or_test_is_not_added_again():
    wide = {"grants": [{"src": ["tag:a", "tag:b"], "dst": ["tag:c"], "ip": ["*"]}],
            "tests": [{"src": "tag:a", "accept": ["tag:c:443"]}, {"src": "tag:a", "deny": ["tag:c:22"]}]}
    want = {"grants": [{"src": ["tag:a"], "dst": ["tag:c"], "ip": ["tcp:443"]}],
            "tests": [{"src": "tag:a", "accept": ["tag:c:443"], "deny": ["tag:c:22"]}]}
    assert merge(wide, want)[1] == []
    app = {"grants": [{**want["grants"][0], "app": {"tailscale.com/cap/kubernetes": [{"impersonate": {}}]}}]}
    assert merge(wide, app)[1] == ["GRANT:tag:a->tag:c:tcp:443+app"]  # an app capability needs its own grant
    assert merge({"tests": [{"src": "tag:a", "proto": "icmp", "accept": ["tag:c:443"]}]},
                 {"tests": want["tests"]})[1] == ["TEST:tag:a"]


def test_merge_adds_service_auto_approvers_and_refuses_unknown_sections():
    want = {"autoApprovers": {"services": {"svc:x": ["tag:x"]}}}
    merged, added, _ = merge({"autoApprovers": {"routes": {"10.0.0.0/8": ["tag:r"]}}}, want)
    assert merged["autoApprovers"] == {"routes": {"10.0.0.0/8": ["tag:r"]}, "services": {"svc:x": ["tag:x"]}}
    assert added == ["AUTO_APPROVER:services:svc:x:tag:x"]
    for bad in ({"groups": {}}, {"autoApprovers": {"exitNode": ["tag:x"]}}):
        with pytest.raises(IacError, match="IAC_FRAGMENT_KEY_UNSUPPORTED"):
            merge({}, bad)
    with pytest.raises(IacError, match="IAC_POLICY_INVALID"):
        merge([], want)


def test_plan_validates_the_merge_and_writes_nothing():
    fake = FakeTailscale(PEOPLE)
    result = sync(client(fake), STATES, "default", apply=False)
    assert result["status"] == "PLANNED" and result["validate"] == "PASS" and result["added"]
    assert [c[1] for c in fake.calls if c[0] == "POST"] == ["validate"]
    assert fake.policy == PEOPLE
    readonly = FakeTailscale(PEOPLE, validate=(403, {}))
    assert sync(client(readonly), STATES, "default", apply=False)["validate"] == "SKIPPED_SCOPE"


def test_apply_writes_on_the_read_etag_and_confirms_by_reading_back():
    fake = FakeTailscale(PEOPLE)
    result = sync(client(fake), STATES, "default", apply=True)
    assert result["status"] == "APPLIED"
    assert ("POST", "acl", '"v1"') in fake.calls and check_policy(fake.policy, STATES) == []
    assert fake.policy["groups"] == PEOPLE["groups"]
    calls = len(fake.calls)
    assert sync(client(fake), STATES, "default", apply=True)["status"] == "NO_CHANGE"
    assert not [c for c in fake.calls[calls:] if c[0] == "POST"]  # nothing to write, not even validate


OLD = {"src": ["tag:example-old"], "dst": ["tag:example-gone"], "ip": ["tcp:443"]}
OLD_TEST = {"src": "tag:example-old", "accept": ["tag:example-gone:443"]}
WITH_OLD = {**WANTED, "tagOwners": {**WANTED["tagOwners"], "tag:example-old": ["autogroup:admin"]},
            "grants": [*WANTED["grants"], OLD], "tests": [*WANTED["tests"], OLD_TEST]}
DROPPED = [i for i, _ in enumerate(STATES) if i != 1]  # source-dev left Git since the baseline


def test_prune_removes_exact_matches_and_reports_what_a_person_changed():
    live = merge(PEOPLE, WITH_OLD)[0]
    pruned, removed, kept = prune(live, WITH_OLD, WANTED)
    assert removed == ["TAG_OWNER:tag:example-old", "GRANT:tag:example-old->tag:example-gone:tcp:443", "TEST:tag:example-old"]
    assert kept == [] and OLD not in pruned["grants"] and pruned["groups"] == PEOPLE["groups"]
    assert all(g in pruned["grants"] for g in PEOPLE["grants"]) and check_policy(pruned, STATES) == []
    assert prune(pruned, WITH_OLD, WANTED)[1:] == ([], [])
    # An exact match is the repository's, whoever wrote it first.
    hand = {**PEOPLE, "grants": [*PEOPLE["grants"], copy.deepcopy(OLD)]}
    assert "GRANT:tag:example-old->tag:example-gone:tcp:443" in prune(hand, WITH_OLD, WANTED)[1]
    # Changed by a person: it stays, and is reported while it still allows or owns what Git dropped.
    widened = {**OLD, "ip": ["tcp:443", "tcp:80"]}
    changed = copy.deepcopy(live)
    changed["grants"] = [widened if g == OLD else g for g in changed["grants"]]
    changed["tagOwners"]["tag:example-old"] = ["autogroup:owner"]
    pruned, removed, kept = prune(changed, WITH_OLD, WANTED)
    assert widened in pruned["grants"] and pruned["tagOwners"]["tag:example-old"] == ["autogroup:owner"]
    assert kept == ["TAG_OWNER:tag:example-old", "GRANT:tag:example-old->tag:example-gone:tcp:443"]
    assert removed == ["TEST:tag:example-old"]


def test_apply_prunes_an_environment_dropped_since_the_baseline():
    states = [STATES[i] for i in DROPPED]
    gone = [g for g in WANTED["grants"] if g not in fragment(states)["grants"]]
    fake = FakeTailscale(merge(PEOPLE, WANTED)[0])
    result = sync(client(fake), states, "default", apply=True, previous_states=STATES)
    assert result["status"] == "APPLIED" and result["removed"] and result["kept"] == [] and gone
    assert check_policy(fake.policy, states) == [] and not [g for g in gone if g in fake.policy["grants"]]
    assert fake.policy["groups"] == PEOPLE["groups"] and all(g in fake.policy["grants"] for g in PEOPLE["grants"])
    assert sync(client(fake), states, "default", apply=True, previous_states=STATES)["status"] == "NO_CHANGE"


def test_a_plan_with_only_removals_is_planned_and_writes_nothing():
    fake = FakeTailscale(merge(PEOPLE, WANTED)[0])
    result = sync(client(fake), [], "default", apply=False, previous_states=STATES)
    assert (result["status"], result["added"]) == ("PLANNED", []) and result["removed"]
    assert not [c for c in fake.calls if c[:2] == ("POST", "acl")]


def test_a_prune_it_cannot_prove_is_refused_or_unknown():
    base = merge(PEOPLE, WANTED)[0]
    failing = FakeTailscale(base, validate=(200, {"message": "test(s) failed"}))
    result = sync(client(failing), [], "default", apply=True, previous_states=STATES)
    assert (result["status"], result["code"]) == ("REFUSED", "IAC_TAILSCALE_VALIDATE_FAILED")
    assert failing.policy == base
    edited = FakeTailscale(base)

    def saved_meanwhile(method, url, headers, body):
        if method == "POST" and url.endswith("/acl"):
            edited.version += 1  # someone saved in the admin console after our read
        return edited(method, url, headers, body)

    result = sync(Client("example.org", "short-lived", send=saved_meanwhile), [], "default", apply=True,
                  previous_states=STATES)
    assert (result["status"], result["code"]) == ("REFUSED", "IAC_POLICY_CHANGED") and edited.policy == base
    deaf = FakeTailscale(base)

    def acknowledged_not_stored(method, url, headers, body):
        if method == "POST" and url.endswith("/acl"):
            return 200, {}, body
        return deaf(method, url, headers, body)

    result = sync(Client("example.org", "short-lived", send=acknowledged_not_stored), [], "default", apply=True,
                  previous_states=STATES)
    assert (result["status"], result["code"]) == ("UNKNOWN", "IAC_APPLY_UNCONFIRMED")


def test_cli_prunes_what_the_previous_dir_had(tmp_path, monkeypatch, capsys):
    fake = FakeTailscale(merge(PEOPLE, WANTED)[0])
    monkeypatch.setattr(tailnet_sync, "github_token", lambda client_id: "short-lived")
    monkeypatch.setattr(tailnet_sync, "Client", lambda name, token: Client(name, token, send=fake))
    previous = tmp_path / "previous"
    previous.mkdir()
    for path in ENVIRONMENTS:
        (previous / path.name).write_text(path.read_text())
    now = [str(ENVIRONMENTS[i]) for i in DROPPED]
    args = ["tailnet-sync", "--tailnet-name", "example.org", "--client-id", "c"]
    assert main([*args, "--previous-dir", str(previous), *now]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "PLANNED" and printed["removed"] and "@" not in json.dumps(printed)
    assert main([*args, *now]) == 0  # without a baseline nothing is removed
    assert json.loads(capsys.readouterr().out)["removed"] == []


def test_apply_refuses_what_it_cannot_prove():
    failing = FakeTailscale(PEOPLE, validate=(200, {"message": "test(s) failed for someone@example.com"}))
    result = sync(client(failing), STATES, "default", apply=True)
    assert (result["status"], result["code"]) == ("REFUSED", "IAC_TAILSCALE_VALIDATE_FAILED")
    assert failing.policy == PEOPLE
    readonly = FakeTailscale(PEOPLE, validate=(403, {}))
    assert sync(client(readonly), STATES, "default", apply=True)["status"] == "REFUSED"
    with pytest.raises(IacError, match="IAC_POLICY_HAS_COMMENTS"):
        sync(client(FakeTailscale(PEOPLE, comments=True)), STATES, "default", apply=True)
    tag = next(iter(WANTED["tagOwners"]))
    conflicted = FakeTailscale({**PEOPLE, "tagOwners": {tag: ["autogroup:member"]}})
    assert sync(client(conflicted), STATES, "default", apply=True)["code"] == "IAC_POLICY_CONFLICT"


def test_a_policy_edited_meanwhile_is_not_overwritten():
    fake = FakeTailscale(PEOPLE)
    real = fake.__call__

    def edited_before_write(method, url, headers, body):
        if method == "POST" and url.endswith("/acl"):
            fake.version += 1  # someone saved in the admin console after our read
        return real(method, url, headers, body)

    result = sync(Client("example.org", "short-lived", send=edited_before_write), STATES, "default", apply=True)
    assert (result["status"], result["code"]) == ("REFUSED", "IAC_POLICY_CHANGED")
    assert fake.policy == PEOPLE


def test_a_lost_answer_is_judged_by_the_read_back_and_never_sent_twice():
    fake = FakeTailscale(PEOPLE, lose_write=True)
    assert sync(client(fake), STATES, "default", apply=True)["status"] == "APPLIED"
    assert len([c for c in fake.calls if c[:2] == ("POST", "acl")]) == 1

    class Unreachable(FakeTailscale):
        def __call__(self, method, url, headers, body):
            if self.version > 1:
                raise TimeoutError  # neither the answer nor the read-back arrives
            return super().__call__(method, url, headers, body)

    gone = Unreachable(PEOPLE, lose_write=True)
    result = sync(client(gone), STATES, "default", apply=True)
    assert (result["status"], result["code"]) == ("UNKNOWN", "IAC_APPLY_UNCONFIRMED")
    assert len([c for c in gone.calls if c[:2] == ("POST", "acl")]) == 1


def test_the_tailnet_is_named_and_output_never_holds_an_email():
    for name in ("", "-", "a/b"):
        with pytest.raises(IacError, match="IAC_TAILNET_NAME_REQUIRED"):
            Client(name, "t")
    assert masked({"code": "x", "added": ["GRANT:someone@example.com->tag:a"]})["added"] == ["GRANT:<user>->tag:a"]


def test_the_token_comes_from_github_oidc(monkeypatch):
    seen = []

    def send(method, url, headers, body):
        seen.append((method, url, body))
        if method == "GET":
            return 200, {}, b'{"value": "github-jwt"}'
        return 200, {}, b'{"access_token": "short-lived", "expires_in": 3600}'

    with pytest.raises(IacError, match="IAC_OIDC_UNAVAILABLE"):
        github_token("client-1", send)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.actions.example/x?api-version=2.0")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-token")
    assert github_token("client-1", send) == "short-lived"
    assert seen[0][1].endswith("&audience=api.tailscale.com%2Fclient-1")
    assert seen[1][1] == "https://api.tailscale.com/api/v2/oauth/token-exchange"
    assert b"client_id=client-1" in seen[1][2] and b"jwt=github-jwt" in seen[1][2]


def test_cli_merges_offline_without_printing_the_policy(tmp_path, capsys):
    live = tmp_path / "live.json"
    live.write_text(json.dumps(PEOPLE))
    out = tmp_path / "merged.json"
    paths = [str(p) for p in ENVIRONMENTS]
    assert main(["tailnet-merge", "--policy", str(live), "--out", str(out), *paths]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["ok"] is True and "@" not in printed
    assert check_policy(json.loads(out.read_text()), STATES) == []
    assert main(["tailnet-sync", "--tailnet-name", "example.org", "--client-id", "c", *paths]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "IAC_OIDC_UNAVAILABLE"
