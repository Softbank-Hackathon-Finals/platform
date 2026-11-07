import json
import urllib.request

import pytest
from fakes import FakeQueue

from bravo_function_runtime.admin import serve
from bravo_function_runtime.config import parse
from bravo_function_runtime.invoke import Kind, Outcome
from bravo_function_runtime.runner import Runner

CONFIG = parse({"bindings": [{"function_name": "f", "handler": "m.h", "queue_name": "q", "batch_size": 2}]})


def _runner(outcome, bodies=("a", "b", "c")):
    q = FakeQueue(bodies=bodies)
    calls = []

    def invoker(binding, evt, ctx, region):
        calls.append([r["messageId"] for r in evt["Records"]])
        return outcome

    return Runner(CONFIG, {"q": q}, invoker), q, calls


def test_success_deletes_the_whole_batch():
    r, q, calls = _runner(Outcome(Kind.OK, 1))
    r.run_once(CONFIG.bindings[0], q)
    assert calls == [["m0", "m1"]] and q.deleted == ["m0", "m1"]


@pytest.mark.parametrize("kind", [Kind.ERROR, Kind.TIMEOUT, Kind.CRASH])
def test_failure_keeps_the_batch_for_redelivery(kind):
    r, q, _ = _runner(Outcome(kind, 1, "x"))
    r.run_once(CONFIG.bindings[0], q)
    assert q.deleted == [] and r.counters.failed == 2


def test_blocked_runner_does_not_claim_new_work():
    r, q, calls = _runner(Outcome(Kind.OK, 1))
    r.set_blocked(True)
    assert r._claim() is False and r.status() == {"write_blocked": True, "in_flight": 0}
    r.set_blocked(False)
    assert r._claim() is True and r.status()["in_flight"] == 1


def _req(port, method, token, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/write-block", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_admin_switch_uses_the_executor_contract():
    r, _, _ = _runner(Outcome(Kind.OK, 1))
    server = serve(r, 0, "secret-token", host="127.0.0.1")
    port = server.server_address[1]
    try:
        assert _req(port, "GET", "wrong")[0] == 403
        assert _req(port, "PUT", "secret-token", {"blocked": True}) == (200, {"write_blocked": True, "in_flight": 0})
        assert _req(port, "GET", "secret-token") == (200, {"write_blocked": True, "in_flight": 0})
        assert _req(port, "PUT", "secret-token", {"blocked": "yes"})[0] == 400
    finally:
        server.shutdown()


def test_admin_refuses_everything_without_a_configured_token():
    r, _, _ = _runner(Outcome(Kind.OK, 1))
    server = serve(r, 0, None, host="127.0.0.1")
    try:
        assert _req(server.server_address[1], "GET", "")[0] == 403
    finally:
        server.shutdown()
