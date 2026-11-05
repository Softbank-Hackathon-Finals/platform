import http.client
import json
import queue
import threading
from http.server import HTTPServer

import httpx
import pytest
from test_subscription_product import setup as setup
from test_subscription_product_runtime import INSTANCE, RUNTIME, SSM
from test_subscription_product_runtime import window_ports as window_ports
from tls_support import TEST_CA_PEM, identity

from bravo_agent import subscription_product_caller as caller
from bravo_agent import subscription_server as server
from bravo_agent.contracts import digest
from bravo_agent.subscription_product_host import ProductInputProbeCall
from bravo_agent.subscription_product_tls import client_context


def test_probe_never_claims_stages_or_invokes_and_does_not_consume_run(setup):
    service, _, body, counts = setup
    before = service.journal.path.read_bytes()
    paths = sorted(service.journal.root.iterdir())
    status, receipt = service.probe(body)
    assert status == 200 and receipt == {
        'state': 'INPUT_AUTHENTICATED', 'runtime_source_sha': RUNTIME,
        'request_digest': digest({'input_ref': body['input_ref'], 'input_digest': body['input_digest']}),
        'product_input_digest': body['input_digest'], 'admission_digest': 'a' * 64,
        'principal_digest': 'b' * 64,
    }
    assert counts == {'auth': 1, 'resolve': 1, 'invoke': 0}
    assert service.journal.path.read_bytes() == before
    assert sorted(service.journal.root.iterdir()) == paths
    assert 'test-identity' not in json.dumps(receipt) and 'test-private' not in json.dumps(receipt)
    assert service.run(body)[1]['state'] == 'PROPOSAL_READY'
    assert counts['invoke'] == 1


@pytest.mark.parametrize('change,status', [
    ({'input_ref': '../outside'}, 400), ({'bundle': {}}, 400),
    ({'web_identity_token': 'wrong'}, 403), ({'input_digest': 'f' * 64}, 409),
    ({'input_ref': 'unknown'}, 409),
])
def test_probe_denies_bad_identity_or_input_without_claim(setup, change, status):
    service, _, body, counts = setup
    before = service.journal.path.read_bytes()
    code, response = service.probe(body | change)
    assert code == status and response['state'] == 'NEEDS_HUMAN'
    assert counts['invoke'] == 0 and service.journal.path.read_bytes() == before
    if status in {400, 403}:
        assert counts['resolve'] == 0
    assert 'secret' not in json.dumps(response)


def test_probe_reauthenticates_and_rechecks_current_reader(setup):
    service, inputs, body, counts = setup
    tokens = []
    service.request_inputs = lambda token: tokens.append(token) or inputs
    assert service.probe(body)[0] == 200
    def revoked(*_):
        raise ValueError('private revoked input')
    inputs.resolve = revoked
    status, result = service.probe(body)
    assert status == 409 and 'private' not in json.dumps(result)
    assert tokens == ['test-identity'] * 2 and counts['auth'] == 2 and counts['invoke'] == 0


def test_probe_does_not_bypass_source_validation(setup):
    service, inputs, body, counts = setup
    inputs.current.files['../escaped'] = 'untrusted source'
    assert service.probe(body)[0] == 409
    assert counts['invoke'] == 0
    assert not (service.journal.root.parent / 'escaped').exists()


def test_http_probe_route_is_bounded_and_does_not_reach_run_or_fixture(monkeypatch, setup, tmp_path):
    service, _, body, counts = setup
    tls, ca_pem = identity(tmp_path)
    ready = queue.Queue()
    def bind(address, handler):
        listener = HTTPServer((address[0], 0), handler)
        ready.put(listener.server_port)
        return listener
    monkeypatch.setattr(server, 'HTTPServer', bind)
    monkeypatch.setattr(service, 'run', lambda *_: pytest.fail('probe cannot execute run'))
    monkeypatch.setattr(server, 'execute_fixture', lambda *_: pytest.fail('probe cannot execute fixture'))
    thread = threading.Thread(target=server.serve, args=({},), kwargs={
        'product': service, 'fixture_enabled': False, 'tls_context': tls}, daemon=True)
    thread.start()
    port = ready.get(timeout=5)
    def post(route, payload):
        connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=5, context=client_context(ca_pem))
        try:
            connection.request('POST', route, json.dumps(payload))
            response = connection.getresponse()
            assert response.getheader('Cache-Control') == 'no-store'
            return response.status, json.load(response)
        finally:
            connection.close()
    assert post('/validation/fixture', {})[0] == 404
    assert post('/input/probe', body | {'bundle': {}})[0] == 400
    assert post('/input/probe', body)[1]['state'] == 'INPUT_AUTHENTICATED'
    thread.join(5)
    assert not thread.is_alive() and counts == {'auth': 1, 'resolve': 1, 'invoke': 0}


def test_fixed_probe_caller_no_proxy_and_digest_only_receipt(monkeypatch, setup):
    service, _, body, counts = setup
    calls = []
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    def transport(request):
        assert str(request.url) == 'https://127.0.0.1:18762/input/probe'
        calls.append(True)
        status, result = service.probe(json.loads(request.content))
        return httpx.Response(status, json=result)
    call = ProductInputProbeCall(RUNTIME, TEST_CA_PEM, httpx.MockTransport(transport))
    assert call(body) == service.probe(body)
    assert calls == [True] and counts['invoke'] == 0


@pytest.mark.parametrize('change', [
    {'state': 'PROPOSAL_READY'}, {'runtime_source_sha': 'd' * 40},
    {'product_input_digest': 'd' * 64}, {'request_digest': 'e' * 64},
    {'principal_digest': 'bad'}, {'admission_digest': 'bad'}, {'private': 'must not escape'},
])
def test_probe_caller_rejects_unbound_or_non_probe_response(setup, change):
    service, _, body, _ = setup
    result = service.probe(body)[1] | change
    call = ProductInputProbeCall(RUNTIME, TEST_CA_PEM, httpx.MockTransport(lambda _: httpx.Response(200, json=result)))
    status, failure = call(body)
    assert status == 502 and 'private' not in json.dumps(failure)


@pytest.mark.parametrize('status,content', [(302, b'private'), (403, b'private'), (200, b'x' * 4097)])
def test_probe_caller_does_not_retry_redirect_or_echo_errors(setup, status, content):
    _, _, body, _ = setup
    calls = []
    def transport(_):
        calls.append(True)
        return httpx.Response(status, content=content, headers={'location': 'https://elsewhere.invalid'})
    code, failure = ProductInputProbeCall(RUNTIME, TEST_CA_PEM, httpx.MockTransport(transport))(body)
    assert code != 200 and len(calls) == 1 and 'private' not in json.dumps(failure)


def test_single_probe_uses_existing_fixed_window_then_cleans_up(setup, window_ports):
    service, _, body, counts = setup
    ssm = SSM()
    def transport(request):
        assert request.url.path == '/input/probe'
        status, result = service.probe(json.loads(request.content))
        return httpx.Response(status, json=result)
    with caller.product_probe_window(INSTANCE, RUNTIME, ca_pem=TEST_CA_PEM, ssm=ssm, transport=httpx.MockTransport(transport)) as probe:
        assert probe(body)[1]['state'] == 'INPUT_AUTHENTICATED'
        assert probe(body)[0] == 409
    assert probe(body)[0] == 409
    assert counts == {'auth': 1, 'resolve': 1, 'invoke': 0}
    assert ssm.calls == ['start', 'session', 'terminate', 'stop']
    assert window_ports == ['plugin-stop']
