"""Tailscale GitOps for the tailnet fragment: merge it into the live policy file, validate, apply.

Git holds only the fragment (`bravo-iac tailnet-policy`); the live policy also holds what Git must not
(groups with member emails, SSH rules, hand-written grants). So the merge is add-only over the four
sections a fragment has, and everything else stays byte-for-byte what the tailnet already has:

- tag owners: a missing tag is added; a tag with other owners is a conflict, never overwritten
- auto approvers, grants, tests: added when the live policy does not already cover them
- prune: an entry the baseline fragment had and this one drops is removed when the live entry is exactly
  that entry, whoever wrote it: an exact match is the repository's. One a person changed stays and is
  reported as kept while it still allows (or still asserts) what Git dropped. The baseline is the commit
  of the last successful develop run (the workflow finds it), so cancelled or refused runs lose nothing

`sync` runs it against the Tailscale API with a short-lived token from GitHub OIDC (workload identity
federation, no stored secret). Plan: read, merge, `check_policy`, Tailscale validate. Apply: the same,
then write with If-Match on the read ETag and read back. A write whose answer is lost is judged by the
read-back, never sent twice. Output carries fixed codes and fragment items only, never policy text.
"""

import copy
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .modules import IacError
from .tailnet import check_policy, fragment

API = "https://api.tailscale.com/api/v2"
SECTIONS = ("tagOwners", "autoApprovers", "grants", "tests")
EMAIL = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]*[A-Za-z0-9])+")
COMMENT = re.compile(r"^\s*(//|/\*)", re.MULTILINE)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _covers(have: dict, want: dict) -> bool:
    """A live grant allows everything the fragment grant does (same app capability, if any)."""
    ips = have.get("ip") or []
    return (all(s in (have.get("src") or []) for s in want["src"])
            and all(d in (have.get("dst") or []) for d in want["dst"])
            and ("*" in ips or all(i in ips for i in want.get("ip", [])))
            and ("app" not in want or _canonical(have.get("app")) == _canonical(want["app"])))


def _tested(tests: list, want: dict) -> bool:
    mine = [t for t in tests if t.get("src") == want["src"] and t.get("proto") == want.get("proto")]
    return all(any(a in t.get(kind, []) for t in mine)
               for kind in ("accept", "deny") for a in want.get(kind, []))


def _item(key, entry) -> str:
    if key == "grants":
        return f"GRANT:{entry['src'][0]}->{entry['dst'][0]}:{entry['ip'][0]}"
    return f"TEST:{entry['src']}"


def prune(live, previous, wanted) -> tuple[dict, list[str], list[str]]:
    """(pruned policy, removed items, kept items): what Git dropped since the baseline `previous`."""
    # ponytail: autoApprovers are not pruned; fragment() emits none. Add when a fragment does.
    pruned, removed, kept = copy.deepcopy(live), [], []
    owners = pruned.get("tagOwners") or {}
    for tag, was in (previous.get("tagOwners") or {}).items():
        if tag in (wanted.get("tagOwners") or {}) or tag not in owners:
            continue
        if owners[tag] == was:
            del owners[tag]
            removed.append(f"TAG_OWNER:{tag}")
        else:
            kept.append(f"TAG_OWNER:{tag}")
    for key in ("grants", "tests"):
        keep = {_canonical(e) for e in wanted.get(key) or []}
        for dropped in [e for e in previous.get(key) or [] if _canonical(e) not in keep]:
            exact = [e for e in pruned.get(key) or [] if _canonical(e) == _canonical(dropped)]
            for entry in exact:
                pruned[key].remove(entry)
            if exact:
                removed.append(_item(key, dropped))
            elif key == "grants" and any(_covers(g, dropped) for g in pruned.get("grants") or []):
                kept.append(_item(key, dropped))  # a person's grant still allows what Git dropped
            elif key == "tests" and _tested(pruned.get("tests") or [], dropped):
                kept.append(_item(key, dropped))
    for key in ("tagOwners", "grants", "tests"):
        if key in pruned and not pruned[key] and key in live and live[key]:
            del pruned[key]  # emptied by the prune: leave no empty section behind
    return pruned, removed, kept


def merge(live, wanted) -> tuple[dict, list[str], list[str]]:
    """(merged policy, added items, conflicts). The live policy is not modified."""
    if type(live) is not dict or type(wanted) is not dict:
        raise IacError("IAC_POLICY_INVALID")
    if set(wanted) - set(SECTIONS):
        raise IacError("IAC_FRAGMENT_KEY_UNSUPPORTED")
    merged, added, conflicts = copy.deepcopy(live), [], []
    for tag, owners in (wanted.get("tagOwners") or {}).items():
        have = (merged.get("tagOwners") or {}).get(tag)
        if have is None:
            merged.setdefault("tagOwners", {})[tag] = list(owners)
            added.append(f"TAG_OWNER:{tag}")
        elif have != owners:
            conflicts.append(f"TAG_OWNER:{tag}")
    for kind, table in (wanted.get("autoApprovers") or {}).items():
        if type(table) is not dict:
            raise IacError("IAC_FRAGMENT_KEY_UNSUPPORTED")
        target = merged.setdefault("autoApprovers", {}).setdefault(kind, {})
        for name, approvers in table.items():
            have = target.setdefault(name, [])
            for approver in approvers:
                if approver not in have:
                    have.append(approver)
                    added.append(f"AUTO_APPROVER:{kind}:{name}:{approver}")
    for grant in wanted.get("grants") or []:
        if not any(_covers(g, grant) for g in merged.get("grants") or []):
            merged.setdefault("grants", []).append(copy.deepcopy(grant))
            app = "+app" if "app" in grant else ""
            added.append(f"GRANT:{grant['src'][0]}->{grant['dst'][0]}:{grant['ip'][0]}{app}")
    for test in wanted.get("tests") or []:
        if not _tested(merged.get("tests") or [], test):
            merged.setdefault("tests", []).append(copy.deepcopy(test))
            added.append(f"TEST:{test['src']}")
    return merged, added, conflicts


class Client:
    """The four Tailscale calls this needs. `send` is (method, url, headers, body) -> (status, headers, body)."""

    def __init__(self, tailnet_name: str, token: str, send=None):
        # The tailnet is named, not "-": a token of another tailnet gets 403/404 instead of a policy.
        if not tailnet_name or tailnet_name == "-" or "/" in tailnet_name:
            raise IacError("IAC_TAILNET_NAME_REQUIRED")
        self.base = f"{API}/tailnet/{urllib.parse.quote(tailnet_name, safe='')}/acl"
        self.token, self.send = token, send or _urllib_send

    def _call(self, method, url, accept="application/json", body=None, extra=None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": accept, **(extra or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body, indent=2).encode()
        return self.send(method, url, headers, body)

    def read(self) -> tuple[dict, str]:
        status, headers, body = self._call("GET", self.base)
        if status != 200:
            raise IacError("IAC_TAILNET_READ_REFUSED")
        etag = {k.lower(): v for k, v in headers.items()}.get("etag")
        if not etag:
            raise IacError("IAC_TAILNET_ETAG_MISSING")
        # Writing JSON would drop HuJSON comments; refuse instead of losing them silently.
        h_status, h_headers, text = self._call("GET", self.base, accept="application/hujson")
        h_etag = {k.lower(): v for k, v in h_headers.items()}.get("etag")
        if h_status != 200 or h_etag != etag:
            raise IacError("IAC_POLICY_CHANGED")
        if COMMENT.search(text.decode("utf-8", "replace")):
            raise IacError("IAC_POLICY_HAS_COMMENTS")
        return json.loads(body), etag

    def validate(self, policy) -> str:
        """PASS, FAIL or SKIPPED_SCOPE (a read-only token may not validate)."""
        status, _, body = self._call("POST", f"{self.base}/validate", body=policy)
        if status in (401, 403):
            return "SKIPPED_SCOPE"
        if status != 200:
            return "FAIL"
        answer = json.loads(body or b"{}")
        return "FAIL" if answer.get("message") or answer.get("data") else "PASS"

    def write(self, policy, etag) -> int:
        status, _, _ = self._call("POST", self.base, body=policy, extra={"If-Match": etag})
        return status


def _urllib_send(method, url, headers, body):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def github_token(client_id: str, send=None) -> str:
    """Exchange this job's GitHub OIDC token for a short-lived Tailscale API token."""
    send = send or _urllib_send
    url, bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not client_id or not url or not bearer:
        raise IacError("IAC_OIDC_UNAVAILABLE")
    audience = urllib.parse.quote(f"api.tailscale.com/{client_id}", safe="")
    status, _, body = send("GET", f"{url}&audience={audience}", {"Authorization": f"Bearer {bearer}"}, None)
    jwt = json.loads(body).get("value") if status == 200 else None
    if not jwt:
        raise IacError("IAC_OIDC_UNAVAILABLE")
    form = urllib.parse.urlencode({"client_id": client_id, "jwt": jwt}).encode()
    status, _, body = send("POST", f"{API}/oauth/token-exchange",
                           {"Content-Type": "application/x-www-form-urlencoded"}, form)
    token = json.loads(body).get("access_token") if status == 200 else None
    if not token:
        raise IacError("IAC_TOKEN_EXCHANGE_REFUSED")
    return token


def sync(client: Client, states, tailnet: str, apply: bool, previous_states=None) -> dict:
    wanted = fragment(states, tailnet)
    previous = fragment(previous_states, tailnet) if previous_states is not None else {}
    live, etag = client.read()
    # Prune first: a dropped entry must not make merge think the new fragment is already covered.
    pruned, removed, kept = prune(live, previous, wanted)
    merged, added, conflicts = merge(pruned, wanted)
    result = {"tailnet": tailnet, "mode": "apply" if apply else "plan", "added": added, "removed": removed,
              "kept": kept, "conflicts": conflicts}
    if conflicts:
        return {**result, "status": "REFUSED", "code": "IAC_POLICY_CONFLICT"}
    if check_policy(merged, states, tailnet):
        return {**result, "status": "REFUSED", "code": "IAC_MERGE_INCOMPLETE"}
    if apply and not added and not removed:
        return {**result, "status": "NO_CHANGE"}
    result["validate"] = client.validate(merged)
    if result["validate"] == "FAIL" or (apply and result["validate"] != "PASS"):
        return {**result, "status": "REFUSED", "code": "IAC_TAILSCALE_VALIDATE_FAILED"}
    if not apply:
        return {**result, "status": "PLANNED" if added or removed else "NO_CHANGE"}
    try:
        written = client.write(merged, etag)
    except OSError:
        written = None  # the answer was lost: the read-back decides, the write is not sent again
    if written == 412:
        return {**result, "status": "REFUSED", "code": "IAC_POLICY_CHANGED"}
    if written not in (200, None):
        return {**result, "status": "REFUSED", "code": "IAC_TAILNET_WRITE_REFUSED"}
    try:
        back, _ = client.read()
        _, again, still = merge(back, wanted)
        _, left, _ = prune(back, previous, wanted)
        confirmed = not again and not still and not left and not check_policy(back, states, tailnet)
    except (IacError, OSError):
        confirmed = False
    if not confirmed:
        return {**result, "status": "UNKNOWN", "code": "IAC_APPLY_UNCONFIRMED"}
    return {**result, "status": "APPLIED"}


def masked(result: dict) -> dict:
    """Fragment items carry tags, never people; mask anyway so a log can never hold an email."""
    return json.loads(EMAIL.sub("<user>", json.dumps(result)))
