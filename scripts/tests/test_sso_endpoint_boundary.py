"""Exercise AWS endpoint resolution with synthetic responses and blocked egress."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts.tests.test_verify_operator_sso import CONFIG, sso

AWS = shutil.which('aws')


class EndpointFixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        # Official HTTPS endpoints stop at this local proxy, before DNS/TLS.
        self.server.blocked_connections += 1
        self.send_error(403)

    def do_GET(self):
        self.server.endpoint_requests += 1
        body = json.dumps({'roleCredentials': {
            'accessKeyId': 'fixture-access-key', 'secretAccessKey': 'fixture-secret',
            'sessionToken': 'fixture-session', 'expiration': 4102444800000,
        }}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.endpoint_requests += 1
        body = b'''<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
<GetCallerIdentityResult><Account>111122223333</Account><UserId>fixture</UserId>
<Arn>arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_BravoOperator_0123456789abcdef/session</Arn>
</GetCallerIdentityResult><ResponseMetadata><RequestId>fixture</RequestId></ResponseMetadata>
</GetCallerIdentityResponse>'''
        self.send_response(200)
        self.send_header('Content-Type', 'text/xml')
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(AWS, 'AWS CLI required for native endpoint resolution')
class SSOEndpointBoundaryTests(unittest.TestCase):
    def test_native_sso_and_sts_ignore_every_configured_endpoint_source(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), EndpointFixture)
        server.endpoint_requests = server.blocked_connections = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.check_endpoint_sources(server)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def check_endpoint_sources(self, server):
        endpoint = 'http://127.0.0.1:' + str(server.server_port)
        commands = [
            ['sso', 'get-role-credentials', '--access-token', 'fixture-token',
             '--account-id', '111122223333', '--role-name', 'BravoOperator'],
            ['sts', 'get-caller-identity'],
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config'
            credentials = Path(directory) / 'credentials'
            credentials.write_text('')
            # No real credentials, SSO cache, HOME changes, or external connections.
            ambient = {
                'PATH': os.environ.get('PATH', ''), 'AWS_SHARED_CREDENTIALS_FILE': str(credentials),
                'AWS_IGNORE_CONFIGURED_ENDPOINT_URLS': 'false', 'AWS_MAX_ATTEMPTS': '1',
                'HTTP_PROXY': endpoint, 'HTTPS_PROXY': endpoint, 'NO_PROXY': '127.0.0.1',
                'http_proxy': endpoint, 'https_proxy': endpoint, 'no_proxy': '127.0.0.1',
            }
            cases = {
                'environment_global': ({'AWS_ENDPOINT_URL': endpoint}, ''),
                'environment_service': ({'AWS_ENDPOINT_URL_SSO': endpoint, 'AWS_ENDPOINT_URL_STS': endpoint}, ''),
                'profile_global': ({}, 'endpoint_url = ' + endpoint + '\n'),
                'profile_services': ({}, 'services = fixture\n'),
            }
            for source, (extra_env, profile_options) in cases.items():
                text = CONFIG.replace('sso_session = bravo',
                                      'ignore_configured_endpoint_urls = false\n' + profile_options + 'sso_session = bravo')
                text += '\n[services fixture]\nsso =\n  endpoint_url = ' + endpoint + '\nsts =\n  endpoint_url = ' + endpoint + '\n'
                config.write_text(text)
                # Use the environment actually handed to AWS by the verifier.
                with patch.dict(os.environ, ambient | extra_env, clear=True), \
                        patch.object(sso.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')) as invoke:
                    sso.run('operator', '111122223333', config)
                    env = invoke.call_args.kwargs['env']
                for command in commands:
                    with self.subTest(source=source, service=command[0]):
                        previous_requests = server.endpoint_requests
                        previous_blocks = server.blocked_connections
                        result = subprocess.run(
                            [AWS, *command, '--profile', 'operator', '--region', 'ap-northeast-2',
                             '--no-sign-request', '--no-cli-pager', '--output', 'json',
                             '--cli-connect-timeout', '2', '--cli-read-timeout', '2'],
                            # One attempt is enough to observe the block; CLI retries
                            # of 2s timeouts could exceed the subprocess bound on CI.
                            env=env | {'AWS_MAX_ATTEMPTS': '1', 'AWS_RETRY_MODE': 'standard'},
                            capture_output=True, text=True, timeout=30, check=False,
                        )
                        self.assertNotEqual(result.returncode, 0, 'Synthetic endpoint must not satisfy the AWS call')
                        self.assertEqual(server.endpoint_requests, previous_requests)
                        self.assertGreater(server.blocked_connections, previous_blocks)


if __name__ == '__main__':
    unittest.main()
