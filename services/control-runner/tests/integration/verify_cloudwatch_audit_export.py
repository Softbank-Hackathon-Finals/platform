"""Actual OTel binary against a synthetic CloudWatch protocol server.

No AWS network, credentials, account or deployed CNPG. Never an AUD-01 receipt.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bravo_controls.cloudwatch_audit import (
    CloudWatchAuditRegistration,
    cloudwatch_collector_config,
)

IMAGE = 'otel/opentelemetry-collector-contrib@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b'
SERVER = r'''
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
root = Path('/fixture')
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        n = int(self.headers.get('Content-Length', '0'))
        if not 0 < n < 2*1024*1024:
            self.send_error(413); return
        data = json.loads(self.rfile.read(n))
        action = self.headers.get('X-Amz-Target', '').split('.')[-1]
        mode = (root/'mode').read_text().strip()
        with (root/'requests.jsonl').open('a') as stream:
            stream.write(json.dumps({'action': action, 'mode': mode, 'body': data})+'\n')
        status, result = 200, {}
        if action == 'CreateLogStream':
            status, result = 400, {'__type': 'ResourceAlreadyExistsException', 'message': 'fixture already exists'}
        elif action == 'PutLogEvents' and mode == 'deny':
            status, result = 400, {'__type': 'AccessDeniedException', 'message': 'fixture permission denied'}
        elif action == 'PutLogEvents' and mode == 'reject':
            result = {'nextSequenceToken': '1', 'rejectedLogEventsInfo': {'tooNewLogEventStartIndex': 0}}
        elif action == 'PutLogEvents' and mode == 'accept':
            result = {'nextSequenceToken': '1'}
            with (root/'accepted.jsonl').open('a') as stream:
                stream.write(json.dumps(data)+'\n')
        else:
            status, result = 400, {'__type': 'AccessDeniedException', 'message': 'fixture unsupported operation'}
        body = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/x-amz-json-1.1')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
'''


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=45, **kwargs).stdout


def rows(path):
    if not path.exists():
        return []
    values = []
    for line in path.read_text().splitlines():
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # an in-progress final append; terminal checks wait for the expected marker
    return values


def wait_for(check, seconds=40):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if check():
            return
        time.sleep(.2)
    raise RuntimeError('collector qualification deadline exceeded')


def run(root, python_image):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', python_image):
        raise ValueError('local Python image content ID required')
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.umask(0o077)
    for d in ('fixture', 'input', 'checkpoints'):
        (root/d).mkdir()
    fixture = root/'fixture'
    (fixture/'server.py').write_text(SERVER)
    (fixture/'mode').write_text('accept')
    binding = CloudWatchAuditRegistration(
        'source', 'source', 'sha256:'+'a'*64, 'source-dev', 'source-db-1',
        '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222',
        ':'.join(('arn', 'aws', 'logs', 'ap-northeast-2', '123456789012', 'log-group', '/bravo/audit/source')),
        ':'.join(('arn', 'aws', 'kms', 'ap-northeast-2', '123456789012', 'key/11111111-1111-4111-8111-111111111111')), 7,
    )
    path = root/'input'/f'{binding.namespace}_{binding.pod}_{binding.pod_uid}'/'postgres'
    path.mkdir(parents=True)
    source = path/'0.log'
    source.touch()
    config = cloudwatch_collector_config(binding)
    # Fixture-only endpoint. Product renderer never accepts an endpoint override.
    config['exporters']['awscloudwatchlogs/audit']['endpoint'] = 'http://cloudwatch-fixture:8080'
    config['service']['telemetry']['logs']['level'] = 'info'
    (root/'config.json').write_text(json.dumps(config))
    suffix = uuid4().hex[:12]
    net, send, recv = ('bravo-t34-cwl-'+suffix+x for x in ('-net', '-send', '-recv'))
    created = []
    receipt = {'collector_image': IMAGE, 'fixture_image': python_image,
               'real_collector': True, 'aws_service': False, 'gate_eligible': False}

    def emit(name):
        marker = 'bravo_audit_'+uuid4().hex
        body = {'logger': 'pgaudit', '_aws': {'logGroupName': 'attacker', 'logStreamName': 'attacker'},
                'log_group_name': 'attacker', 'log_stream_name': 'attacker',
                'private-field': 'must-not-leak', 'record': {
                    'user_name': 'postgres', 'database_name': 'chat', 'application_name': marker,
                    'query': 'must-not-leak', 'audit': {'audit_type': 'SESSION', 'class': 'DDL',
                    'object_type': 'TABLE', 'object_name': 'probe.'+marker,
                    'command': 'CREATE TABLE', 'statement': 'must-not-leak'}}}
        timestamp = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
        with source.open('a') as stream:
            stream.write(timestamp+' stdout F '+json.dumps(body)+'\n')
        return marker

    def accepted(marker):
        return any(marker in event['message'] for batch in rows(fixture/'accepted.jsonl')
                   for event in batch['logEvents'])

    def requested(marker, mode):
        return any(item['mode'] == mode and marker in json.dumps(item['body'])
                   for item in rows(fixture/'requests.jsonl') if item['action'] == 'PutLogEvents')

    try:
        command('docker', 'network', 'create', '--internal', '--label', 'bravo.t34.run='+suffix, net)
        created.append(('network', net))
        common = ['docker', 'run', '-d', '--network', net, '--read-only', '--cap-drop=ALL',
                  '--security-opt=no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
                  '--memory', '256m', '--cpus', '1', '--pids-limit', '64',
                  '--label', 'bravo.t34.run='+suffix]
        command(*common, '--name', recv, '--network-alias', 'cloudwatch-fixture',
                '--entrypoint', 'python3', '-v', f'{fixture}:/fixture', python_image,
                '-B', '/fixture/server.py')
        created.append(('container', recv))
        command(*common, '--name', send, '-v', f'{root}/config.json:/etc/config.json:ro',
                '-v', f'{root}/input:/var/log/pods:ro',
                '-v', f'{root}/checkpoints:/var/lib/bravo-audit/checkpoints',
                '-e', 'AWS_ACCESS_KEY_ID=fixture', '-e', 'AWS_SECRET_ACCESS_KEY=fixture',
                '-e', 'AWS_EC2_METADATA_DISABLED=true', IMAGE, '--config=/etc/config.json')
        created.append(('container', send))
        first = emit('normal')
        wait_for(lambda: accepted(first))
        receipt['normal_delivery'] = True
        batches = rows(fixture/'accepted.jsonl')
        assert all(x['logGroupName'] == binding.group and x['logStreamName'] == binding.stream for x in batches)
        assert 'must-not-leak' not in json.dumps(batches)
        assert 'attacker' not in json.dumps(batches)
        assert all(isinstance(json.loads(e['message']), dict) for b in batches for e in b['logEvents'])
        receipt['routing_injection_and_sql_removed'] = True
        # Verify actual consumer parser compatibility, not only JSON syntax.
        message = next(e['message'] for b in batches for e in b['logEvents'] if first in e['message'])
        assert json.loads(message)['record']['application_name'] == first
        receipt['raw_cnpg_shape_preserved'] = True
        (fixture/'mode').write_text('deny')
        pending = emit('denied')
        wait_for(lambda: requested(pending, 'deny'))
        assert not accepted(pending)
        command('docker', 'stop', '-t', '3', send)
        assert any((root/'checkpoints').iterdir())
        (fixture/'mode').write_text('accept')
        command('docker', 'start', send)
        wait_for(lambda: accepted(pending))
        receipt['denied_queue_recovers_after_restart'] = True
        # AWS can return HTTP200 while rejecting records. Exporter success is not
        # canary-search success; preserve this measured limitation in evidence.
        (fixture/'mode').write_text('reject')
        rejected = emit('partial-rejection')
        wait_for(lambda: requested(rejected, 'reject'))
        time.sleep(2)
        assert not accepted(rejected)
        receipt['http200_rejected_canary_absent'] = True
        command('docker', 'stop', '-t', '3', send)
        logs = subprocess.run(['docker', 'logs', send], text=True, capture_output=True, timeout=10)
        (root/'collector.log').write_text(logs.stdout+logs.stderr)
        requests = rows(fixture/'requests.jsonl')
        assert set(x['action'] for x in requests) <= {'CreateLogStream', 'PutLogEvents'}
        receipt['only_exact_stream_calls'] = True
        receipt['request_count'] = len(requests)
        receipt['accepted_batches'] = len(rows(fixture/'accepted.jsonl'))
        receipt['completed_at'] = datetime.now(UTC).isoformat()
    finally:
        errors = []
        for kind, name in reversed(created):
            args = ['docker', 'rm', '-f', name] if kind == 'container' else ['docker', 'network', 'rm', name]
            result = subprocess.run(args, capture_output=True, text=True, timeout=30)
            if result.returncode and 'no such' not in result.stderr.lower():
                errors.append(kind)
        if errors:
            raise RuntimeError('owned fixture cleanup incomplete: '+','.join(errors))
    receipt['owned_resources_removed'] = True
    payload = json.dumps(receipt, sort_keys=True).encode()
    (root/'receipt.json').write_bytes(payload)
    print(json.dumps({'receipt_sha256': hashlib.sha256(payload).hexdigest(), **receipt}))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--private-output', type=Path, required=True)
    p.add_argument('--python-image', required=True)
    a = p.parse_args()
    run(a.private_output.resolve(), a.python_image)
