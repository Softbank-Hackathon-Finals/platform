import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from test_subscription_product import setup as setup
from test_subscription_product_runtime import RUNTIME
from tls_support import identity

from bravo_agent import subscription_product_server as product_server
from bravo_agent import subscription_server
from bravo_agent.subscription_product_host import ProductInputProbeCall, ProductTunnelCall
from bravo_agent.subscription_product_tls import client_context, server_context


@contextmanager
def listener(service, seen, tls=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            seen.append(raw)
            code, response = (service.run if self.path == '/run' else service.probe)(json.loads(raw))
            data = json.dumps(response).encode()
            self.send_response(code)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with HTTPServer(('127.0.0.1', 18762), Handler) as server:
        if tls:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join(5)
            assert not thread.is_alive()


@pytest.mark.parametrize('port', [ProductTunnelCall, ProductInputProbeCall])
@pytest.mark.parametrize('fault', ['none', 'plain', 'wrong_ca', 'wrong_name', 'expired'])
def test_actual_receiver_authentication_before_token_body(setup, tmp_path, monkeypatch, port, fault):
    service, _, body, counts = setup
    tls, pem = identity(tmp_path / 'server', correct_name=fault != 'wrong_name', expired=fault == 'expired')
    if fault == 'wrong_ca':
        _, pem = identity(tmp_path / 'trusted')
    seen = []
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'missing'))
    with listener(service, seen, None if fault == 'plain' else tls):
        status, result = port(RUNTIME, pem)(body)
    if fault == 'none':
        assert status == 200 and len(seen) == 1
        assert counts['invoke'] == (port is ProductTunnelCall)
    else:
        assert status == 502 and seen == []
        assert counts == {'auth': 0, 'resolve': 0, 'invoke': 0}
        assert body['web_identity_token'] not in json.dumps(result)






def test_server_requires_tls_before_bind(monkeypatch, setup):
    monkeypatch.setattr(subscription_server, 'HTTPServer', lambda *_: pytest.fail('plain listener opened'))
    with pytest.raises(ValueError, match='authenticated product TLS'):
        subscription_server.serve({}, product=setup[0])


def test_private_credential_mount_and_missing_key_close_startup(tmp_path, monkeypatch):
    _, pem = identity(tmp_path)
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    assert server_context().minimum_version >= client_context(pem).minimum_version
    (tmp_path / 'broker.key').unlink()
    with pytest.raises(FileNotFoundError):
        server_context()
    settings, reader = tmp_path / 'settings.json', tmp_path / 'reader.json'
    settings.write_text('{}')
    reader.write_text(json.dumps({'origin': 'https://inputs.invalid', 'ca_file': str(tmp_path / 'broker.crt')}))
    monkeypatch.setattr(product_server, 'build_service', lambda *_: pytest.fail('journal before TLS credential check'))
    with pytest.raises(FileNotFoundError):
        product_server.main(settings, reader)
