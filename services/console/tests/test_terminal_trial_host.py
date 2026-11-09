"""Checkout-only host auth boundaries with fixture manager; no cluster actions."""

import hashlib
import hmac
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from test_kubernetes_workload import case as case

from bravo_console.observation_console import ObservationConsole

path = Path(__file__).parents[1] / "testing/terminal_trial_host.py"
spec = importlib.util.spec_from_file_location("terminal_trial_host_fixture", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def local(case):
    _, _, source, _ = case
    access = [True]
    owner = [True]  # control is a separate, owner-only decision (#905 review 1)
    calls = []
    console = ObservationConsole(
        source, origin="http://127.0.0.1:18126", authorize=lambda _: access[0]
    )
    manager = SimpleNamespace(
        status=lambda: {"session_id": "owned-session", "plan_digest": "d" * 64},
        act=lambda value: calls.append(value) or {"accepted": True},
    )
    app = module.TerminalTrialHost(
        console,
        manager,
        authorize=lambda _: access[0],
        authorize_control=lambda _: access[0] and owner[0],
        terminal_command="sova-trial attempt",
    )
    app.owner = owner
    return app, TestClient(app, base_url=console.origin), access, calls


def control(action="prepare", **changes):
    return {
        "action": action,
        "request_id": "a" * 32,
        "session_id": "owned-session",
        "plan_digest": "d" * 64,
        "approved": True,
    } | changes


def test_fixed_trial_host_keeps_native_mutations_closed(local):
    app, client, _, _ = local
    assert "/__trial/client.js" in client.get("/console/").text
    assert client.get("/__trial/status").json()["authorizes_execution"] is False
    assert client.get("/__trial/client.js").status_code == 200
    assert client.post("/v1/plans/any/cutover", json={}).status_code == 405
    assert client.post("/v1/environments", json={}).status_code == 405


def test_explicit_local_auth_and_current_csrf_are_required(local):
    app, client, access, calls = local
    body = control()
    assert client.post("/__trial/control", json=body).status_code == 403
    headers = {"Origin": app.console.origin, "X-Sova-Trial": app.csrf}
    assert client.post("/__trial/control", json=body, headers=headers).status_code == 202
    assert len(calls) == 1
    access[0] = False
    assert client.get("/__trial/client.js").status_code == 200
    assert client.get("/__trial/status").status_code == 403
    assert client.get("/__trial/feed").status_code == 403
    assert client.post("/__trial/control", json=body, headers=headers).status_code == 403
    assert len(calls) == 1


@pytest.mark.parametrize(
    "path,headers",
    [
        ("/__trial/status", {"Origin": "https://other.invalid"}),
        ("/__trial/status?file=other", {}),
        ("/__trial/status", {"Host": "other.invalid"}),
        ("/__trial/status", {"Sec-Fetch-Site": "cross-site"}),
        # another 127.0.0.1 port is same-site and cookies are not port-scoped (#905 review 3)
        ("/__trial/status", {"Sec-Fetch-Site": "same-site"}),
        ("/__trial/status", {"Sec-Fetch-Site": "none"}),
    ],
)
def test_foreign_origin_host_or_path_cannot_obtain_trial_scope(local, path, headers):
    _, client, _, _ = local
    assert client.get(path, headers=headers).status_code == 403


def test_browser_cannot_submit_an_attack_command_or_target(local):
    app, client, _, calls = local
    response = client.post(
        "/__trial/control",
        json={"action": "attempt", "url": "https://outside.invalid"},
        headers={"Origin": app.console.origin, "X-Sova-Trial": app.csrf},
    )
    assert response.status_code == 409 and calls == []


@pytest.mark.parametrize(
    "change",
    [None, "signature", "plan", "synthetic", "authority", "public_file", "older_observation"],
)
def test_signed_trial_feed_is_scope_bound_and_never_execution_authority(tmp_path, change):
    root = tmp_path / "state"
    root.mkdir()
    doc = json.loads((Path(__file__).parent / "fixtures/workload-synthetic.json").read_text())
    at = datetime.now(UTC) - timedelta(seconds=1)
    until = at + timedelta(seconds=60)
    doc.update(provenance="live", observed_at=at.isoformat(), expires_at=until.isoformat())
    for proof in doc["evidence"]:
        proof.update(observed_at=at.isoformat(), expires_at=until.isoformat())
    for event in doc["events"]:
        event["occurred_at"] = at.isoformat()
    binding = {k: doc[k] for k in ["plan_id", "plan_digest", "plan_version"]}
    if change == "plan":
        doc["plan_digest"] = "a" * 64
    if change == "synthetic":
        doc["provenance"] = "synthetic"
    if change == "authority":
        doc["authorizes_execution"] = True
    key = b"x" * 32
    envelope = {
        "producer_id": "owned-onprem-qualification",
        "source_revision": "a" * 40,
        "body": doc,
    }
    signature = hmac.new(key, module.canonical(envelope), hashlib.sha256).hexdigest()
    if change == "signature":
        signature = "b" * 64
    files = {
        "registration.private.json": module.canonical(binding),
        "trial.private.json": module.canonical(
            {"source_revision": "a" * 40, "qualification_only": True}
        ),
        "feed-key.private": key,
        "feed.private.json": module.canonical(envelope | {"hmac_sha256": signature}),
    }
    for name, raw in files.items():
        p = root / name
        p.write_bytes(raw)
        p.chmod(0o600)
    if change == "public_file":
        (root / "feed.private.json").chmod(0o644)
    host = object.__new__(module.TerminalTrialHost)
    host.manager = SimpleNamespace(
        status=lambda: {
            "session_id": "owned-session",
            "observation_revision": doc["revision"] + (change == "older_observation"),
        },
        session=tmp_path,
        settings=SimpleNamespace(producer_revision="a" * 40),
    )
    if change:
        with pytest.raises(ValueError):
            host.feed()
    else:
        value = host.feed()
        assert value["plan_connected"] is False and value["authorizes_execution"] is False


def test_a_session_that_may_watch_cannot_act(local):
    app, client, _, calls = local
    app.owner[0] = False
    headers = {"Origin": app.console.origin, "X-Sova-Trial": app.csrf}
    assert client.get("/__trial/status").status_code == 200
    response = client.post("/__trial/control", json=control(), headers=headers)
    assert response.status_code == 403 and response.json()["error"] == "trial_owner_required"
    assert calls == []


def test_read_and_control_authority_must_be_distinct(case):
    _, _, source, _ = case
    console = ObservationConsole(source, origin="http://127.0.0.1:18126", authorize=lambda _: True)
    same = lambda _: True  # noqa: E731
    with pytest.raises(ValueError, match="explicit_owned_trial_composition_required"):
        module.TerminalTrialHost(
            console, SimpleNamespace(status=dict), authorize=same, authorize_control=same,
            terminal_command="sova-trial attempt",
        )


@pytest.mark.parametrize(
    "body",
    [
        control(session_id="older-session"),
        control(plan_digest="e" * 64),
        control("respond", approved=False),
        control(approved="true"),
        control(request_id="not-hex"),
        control() | {"target": "https://outside.invalid"},
        {k: v for k, v in control().items() if k != "approved"},
    ],
)
def test_the_host_binds_control_to_the_current_session_and_consent(local, body):
    # Before #905 review 2 the host checked only `action`; the fixture manager accepted the rest.
    app, client, _, calls = local
    headers = {"Origin": app.console.origin, "X-Sova-Trial": app.csrf}
    assert client.post("/__trial/control", json=body, headers=headers).status_code == 409
    assert calls == []


def test_stop_needs_no_consent_but_still_the_current_session(local):
    app, client, _, calls = local
    headers = {"Origin": app.console.origin, "X-Sova-Trial": app.csrf}
    assert client.post("/__trial/control", json=control("stop", approved=False), headers=headers).status_code == 202
    assert calls[-1]["action"] == "stop"
