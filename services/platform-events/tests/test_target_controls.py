"""ADR-0041: the platform records the post-cutover target T14 run, at most once per attempt window."""

import contextlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bravo_platform_events import target_controls
from bravo_platform_events.target_controls import Kube, TargetRunProducer, watch_preparer

FINISHED = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
CONTEXT = SimpleNamespace(target_environment_id="env-target", profile_id="apply-recheck", profile_version="1")
SPEC = {"workload_id": "demo-chat"}


class Store:
    def __init__(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.execute("CREATE TABLE plan_commands (plan_id, command_key, request_json, receipt_json)")

    @contextlib.contextmanager
    def _transaction(self, **_):
        yield self.c

    def run(self, started_at, *, side="target", environment="env-target"):
        receipt = {"side": side, "ref": "r-" + started_at, "document": {"environment": {"id": environment},
                                                                       "started_at": started_at}}
        self.c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                       ("p1", "gate-run-" + started_at, "{}", json.dumps(receipt)))


class Records:
    def __init__(self):
        self.calls = []

    def record_run(self, principal, plan_id, side, snapshot):
        self.calls.append((principal, plan_id, side, snapshot.ref, snapshot.document))
        return {"ref": snapshot.ref, "digest": snapshot.fingerprint}


def producer(store, records, measured, *, located=(CONTEXT, SPEC, FINISHED), clock=None):
    def measure(**kw):
        measured.append(kw)
        return {"run_id": "run-1", "app": kw["app"], "environment": {"id": kw["environment_id"], "role": "target"}}

    return TargetRunProducer(store, records, lambda tenant: ("host", tenant), measure=measure,
                             locate=lambda c, plan_id: located, monotonic=clock or (lambda: 0.0))


def test_records_one_target_run_for_the_gate_context_profile_after_the_cutover():
    store, records, measured = Store(), Records(), []
    store.run("2026-11-07T09:59:59Z")  # before the cutover finished: does not count
    assert producer(store, records, measured).ensure("p1", "bravo") == "run-1"
    assert measured == [{"profile": ("apply-recheck", "1"), "app": "demo-chat", "environment_id": "env-target"}]
    principal, plan_id, side, ref, document = records.calls[0]
    assert (principal, plan_id, side, ref) == (("host", "bravo"), "p1", "target", "run-1")
    assert document["environment"]["id"] == "env-target"


def test_an_existing_post_cutover_target_run_is_not_measured_again():
    store, records, measured = Store(), Records(), []
    store.run("2026-11-07T10:00:01Z")
    store.run("2026-11-07T10:00:02Z", side="source")
    assert producer(store, records, measured).ensure("p1", "bravo") is None
    assert measured == [] and records.calls == []


def test_only_a_watch_plan_with_a_gate_context_is_measured():
    store, records, measured = Store(), Records(), []
    assert producer(store, records, measured, located=None).ensure("p1", "bravo") is None
    assert measured == []


def test_a_failed_attempt_waits_for_the_retry_window():
    store, records, measured, now = Store(), Records(), [], [0.0]
    p = producer(store, records, measured, clock=lambda: now[0])

    def fail(**kw):
        measured.append(kw)
        raise RuntimeError("port_forward_failed:seaweedfs")

    p.measure = fail
    with pytest.raises(RuntimeError):
        p.ensure("p1", "bravo")
    now[0] = target_controls.RETRY_SECONDS - 1
    assert p.ensure("p1", "bravo") is None and len(measured) == 1  # no hammering every sweep
    now[0] = target_controls.RETRY_SECONDS + 1
    with pytest.raises(RuntimeError):
        p.ensure("p1", "bravo")
    assert len(measured) == 2


def test_watch_still_reads_when_the_producer_fails():
    class Broken:
        def ensure(self, *_):
            raise RuntimeError("target_api_refused:create")

    assert watch_preparer(Broken(), lambda plan_id, tenant, version: ("reader", version))("p1", "bravo", 7) == ("reader", 7)


def _fake_kubectl(tmp_path, body):
    script = tmp_path / "kubectl"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_port_forward_yields_the_local_port_and_stops_the_process(tmp_path):
    marker = tmp_path / "stopped"
    binary = _fake_kubectl(tmp_path, f"trap 'touch {marker}; exit 0' TERM\n"
                                     "echo 'Forwarding from 127.0.0.1:45678 -> 8333'\nwhile :; do sleep 0.1; done\n")
    with Kube("/k", binary=binary, timeout=5).forward("target-dev", "seaweedfs", 8333) as port:
        assert port == 45678
    for _ in range(50):
        if marker.exists():
            break
        os.system("sleep 0.1")
    assert marker.exists()


def test_port_forward_failure_is_an_error_not_a_port(tmp_path):
    binary = _fake_kubectl(tmp_path, "echo 'error: services \"x\" not found' >&2\nexit 1\n")
    with pytest.raises(RuntimeError, match="port_forward_failed"):
        with Kube("/k", binary=binary, timeout=2).forward("target-dev", "x", 1):
            pass


def test_tokens_are_minted_from_outside_with_a_fixed_audience_and_lifetime(tmp_path):
    binary = _fake_kubectl(tmp_path, 'echo "$@"\n')
    args = Kube("/k", binary=binary).token("target-dev", "demo-chat").split()
    assert args[args.index("--audience") + 1] == "openbao" and args[args.index("--duration") + 1] == "600s"
    assert args[:2] == ["--kubeconfig", "/k"] and "create" in args and "demo-chat" in args


def test_configuration_accepts_only_wired_adapters(tmp_path):
    config = tmp_path / "target-controls.json"
    base = {"kubeconfig": "/k", "evidence_dir": str(tmp_path), "environment_ref": "target/onprem"}
    config.write_text(json.dumps(base | {"tests": {"AUD-01.1": {"adapter": "pod-log"}}}))  # no DB-admin collector
    with pytest.raises(ValueError, match="target_controls_configuration_invalid"):
        target_controls.create(None, config)


def _kubeconfig(tmp_path, claims):
    import base64

    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    token = ".".join([part({"alg": "RS256"}), part(claims), "signature"])
    path = tmp_path / "kubeconfig"
    path.write_text(json.dumps({"users": [{"name": "collector", "user": {"token": token}}]}))
    return path


@pytest.mark.parametrize("claims, ok", [
    ({"iat": 1000, "exp": 1000 + 24 * 3600}, True),                 # the documented 24 h token
    ({"iat": 1000, "exp": 1000 + 24 * 3600 + 1}, False),            # longer than 24 h
    ({"iat": 1000}, False),                                         # no exp: a legacy/unbounded token
    ({"iat": 1000, "exp": 1500}, False),                            # expires before one measurement ends
])
def test_the_collector_credential_must_be_short_lived(tmp_path, claims, ok):
    path = _kubeconfig(tmp_path, claims)
    if ok:
        assert target_controls.bounded_credential(path, now=lambda: 1000) == claims["exp"]
    else:
        with pytest.raises(RuntimeError, match="collector_credential_unbounded"):
            target_controls.bounded_credential(path, now=lambda: 1000)


def test_psql_must_be_the_image_s_own_18_x(tmp_path):
    good = _fake_kubectl(tmp_path, 'echo "psql (PostgreSQL) 18.6 (Debian 18.6-1.pgdg13+2)"\n')
    sha, version = target_controls.psql_qualification(good)
    assert sha.startswith("sha256:") and len(sha) == 71 and version == "18.6 (Debian 18.6-1.pgdg13+2)"
    (tmp_path / "old").mkdir()
    old = _fake_kubectl(tmp_path / "old", 'echo "psql (PostgreSQL) 17.2"\n')
    with pytest.raises(RuntimeError, match="psql_18_required"):
        target_controls.psql_qualification(old)


def test_enc_is_a_wired_adapter_and_reads_its_login_through_the_openbao_entry(tmp_path):
    config = tmp_path / "target-controls.json"
    base = {"kubeconfig": "/k", "evidence_dir": str(tmp_path), "environment_ref": "target/onprem"}
    config.write_text(json.dumps(base | {"tests": {"ENC-01.1": {"adapter": "pooler-tls"}}}))
    assert target_controls.create(None, config).measure is not None
    assert target_controls.ADAPTERS["ENC-01.1"] == "pooler-tls" and target_controls.POOLER_CLIENT == "port-forward"
