import copy
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_subscription_product import setup as setup
from tls_support import TEST_CA_PEM

from bravo_agent.product_execution_contracts import (
    ARTIFACT,
    REPOSITORY,
    WORKFLOW,
    ExecutionIntent,
    receipt_binding,
    validate_execution_receipt,
)
from bravo_agent.subscription_product_dispatch import (
    DispatchUnconfirmed,
    ExecutionPending,
    ExecutionUnknown,
    OperatorGitHub,
    OperatorProductExecution,
)
from bravo_agent.subscription_product_execution import execute
from bravo_agent.subscription_product_seal import seal_receipt


def intent_for(body, mode='propose'):
    return ExecutionIntent(request_id='execution-1', input_ref=body['input_ref'], input_digest=body['input_digest'],
                           admission_digest='a' * 64, runtime_source_sha='c' * 40, mode=mode,
                           recipient_public_key=X25519PrivateKey.from_private_bytes(b'1' * 32).public_key().public_bytes_raw().hex())


def environment(intent):
    return {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_REPOSITORY_ID': '1443045223',
            'GITHUB_REPOSITORY_OWNER_ID': '350259663', 'GITHUB_REF': 'refs/heads/develop',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '1234',
            'GITHUB_SHA': intent.runtime_source_sha, 'PRODUCT_WORKFLOW_SHA': intent.runtime_source_sha,
            'GITHUB_WORKFLOW_REF': f'{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/develop',
            'BRAVO_INSTANCE_ID': 'i-' + 'a' * 17, 'PRODUCT_BROKER_CA_PEM': TEST_CA_PEM}


def service_window(service, mode, seen, *, fail_cleanup=False):
    @contextmanager
    def window(*_, ca_pem):
        assert ca_pem == TEST_CA_PEM
        seen.append('open')
        def call(body):
            seen.append('call')
            return service.probe(body) if mode == 'probe' else service.run(body)
        try:
            yield call
        finally:
            seen.append('cleanup')
            if fail_cleanup:
                raise RuntimeError('private credential detail')
    return window


@pytest.mark.parametrize('mode', ['probe', 'propose'])
def test_one_workflow_call_and_cleanup_before_receipt(setup, mode):
    service, _, body, counts = setup
    intent, seen = intent_for(body, mode), []
    result = execute(intent, environment(intent), token_supplier=lambda: body['web_identity_token'],
                     window=service_window(service, mode, seen))
    assert validate_execution_receipt(intent, 1234, result) == result
    assert seen == ['open', 'call', 'cleanup'] and counts['invoke'] == (mode == 'propose')
    assert 'test-identity' not in json.dumps(result) and 'test-private' not in json.dumps(result)


@pytest.mark.parametrize('change', [
    {'GITHUB_RUN_ATTEMPT': '2'}, {'GITHUB_REPOSITORY_ID': 'wrong'}, {'GITHUB_SHA': 'd' * 40},
    {'PRODUCT_WORKFLOW_SHA': 'd' * 40}, {'GITHUB_EVENT_NAME': 'pull_request'},
    {'GITHUB_REF': 'refs/heads/main'}, {'GITHUB_WORKFLOW_REF': 'different'}, {'GITHUB_RUN_ID': ''},
])
def test_wrong_workflow_cannot_acquire_token_or_open_window(setup, change):
    intent = intent_for(setup[2])
    with pytest.raises(ValueError):
        execute(intent, environment(intent) | change,
                token_supplier=lambda: pytest.fail('token before workflow validation'),
                window=lambda *_: pytest.fail('window before workflow validation'))


def test_cleanup_unknown_discards_otherwise_valid_proposal(setup):
    service, _, body, counts = setup
    intent, seen = intent_for(body), []
    receipt = execute(intent, environment(intent), token_supplier=lambda: body['web_identity_token'],
                      window=service_window(service, 'propose', seen, fail_cleanup=True))
    assert counts['invoke'] == 1 and seen == ['open', 'call', 'cleanup']
    assert receipt['status'] == 502 and receipt['cleanup_confirmed'] is False
    assert receipt['result']['state'] == 'NEEDS_HUMAN' and 'proposal' not in receipt['result']
    assert 'private' not in json.dumps(receipt)


@pytest.mark.parametrize('key,value', [('github_run_id', 'other'), ('github_run_attempt', 2),
    ('cleanup_confirmed', False), ('status', True), ('admission_digest', 'f' * 64), ('mode', 'probe')])
def test_receipt_binding_refuses_other_run_attempt_or_admission(setup, key, value):
    service, _, body, _ = setup
    intent = intent_for(body)
    receipt = receipt_binding(intent, 1234) | {'status': 200, 'cleanup_confirmed': True, 'result': service.run(body)[1]}
    with pytest.raises(ValueError):
        validate_execution_receipt(intent, 1234, receipt | {key: value})


class GitHub:
    def __init__(self, intent, receipt):
        self.intent, self.posts, self.lose_response = intent, [], False
        self.run = {'id': 1234, 'event': 'workflow_dispatch', 'head_branch': 'develop', 'head_sha': intent.runtime_source_sha,
                    'run_attempt': 1, 'workflow_id': 42, 'actor': {'id': 7},
                    'head_repository': {'full_name': REPOSITORY}, 'display_title': 'product-' + intent.request_id,
                    'status': 'completed', 'conclusion': 'success'}
        self.runs = [self.run]
        self.set_artifact(receipt)

    def set_artifact(self, receipt, name='product-result.json'):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr(name, json.dumps(seal_receipt(self.intent, 1234, receipt)))
        self.raw = output.getvalue()
        self.artifact = {'id': 88, 'name': ARTIFACT, 'expired': False, 'size_in_bytes': len(self.raw),
                         'workflow_run': {'id': 1234, 'head_sha': self.intent.runtime_source_sha},
                         'digest': 'sha256:' + hashlib.sha256(self.raw).hexdigest()}

    def get(self, path):
        if path == 'user':
            return {'id': 7}
        if path.endswith('git/ref/heads/develop'):
            return {'object': {'sha': self.intent.runtime_source_sha}}
        if path.endswith('/workflows/' + WORKFLOW):
            return {'id': 42, 'path': '.github/workflows/' + WORKFLOW, 'state': 'active'}
        if '/workflows/42/runs?' in path:
            return {'workflow_runs': copy.deepcopy(self.runs)}
        if path.endswith('/runs/1234'):
            return copy.deepcopy(self.run)
        if path.endswith('/runs/1234/artifacts?per_page=100'):
            return {'total_count': 1, 'artifacts': [copy.deepcopy(self.artifact)]}
        raise AssertionError(path)

    def dispatch(self, workflow_id, inputs):
        assert workflow_id == 42 and inputs == self.intent.model_dump()
        assert 'token' not in inputs
        self.posts.append(inputs)
        if self.lose_response:
            raise RuntimeError('private transport failure')
        return getattr(self, 'response', None)

    def download(self, artifact_id):
        assert artifact_id == 88
        return self.raw


@pytest.fixture
def dispatch(setup, tmp_path):
    service, _, body, _ = setup
    client = OperatorProductExecution(tmp_path / 'state')
    intent = intent_for(body).model_copy(update={'recipient_public_key': client.recipient('execution-1')})
    receipt = receipt_binding(intent, 1234) | {'status': 200, 'cleanup_confirmed': True, 'result': service.run(body)[1]}
    gh = GitHub(intent, receipt)
    client.github = gh
    return client, gh, intent, receipt


def test_submit_restart_and_read_use_same_provider_run_only(dispatch):
    client, gh, intent, expected = dispatch
    assert client.submit(intent) == expected
    restarted = OperatorProductExecution(client.root, gh)
    assert restarted.submit(intent) == restarted.read(intent) == expected
    assert len(gh.posts) == 1
    assert b'test-identity' not in client.path.read_bytes()


def test_lost_dispatch_response_remains_read_only_after_restart(dispatch):
    client, gh, intent, expected = dispatch
    gh.lose_response = True
    with pytest.raises(ExecutionUnknown, match='DISPATCH_RESPONSE_UNCONFIRMED'):
        client.submit(intent)
    assert OperatorProductExecution(client.root, gh).submit(intent) == expected
    assert len(gh.posts) == 1


def test_rejected_dispatch_keeps_private_http_evidence_and_cannot_resend(dispatch, monkeypatch):
    client, gh, intent, _ = dispatch
    calls = []
    body = '{"message":"fixture provider validation rejection","status":"422"}'
    def call(args, **kwargs):
        calls.append(args)
        assert json.loads(kwargs['input']) == {'ref': 'develop', 'inputs': intent.model_dump(), 'return_run_details': True}
        return subprocess.CompletedProcess(args, 1,
            ('HTTP/2.0 422 Unprocessable Entity\r\nX-Github-Request-Id: A:1:B:2\r\n'
             'Location: https://example.invalid/private-link\r\n\r\n' + body).encode(),
            b'private authentication diagnostic')
    monkeypatch.setattr(subprocess, 'run', call)
    gh.dispatch = OperatorGitHub().dispatch
    gh.runs = []
    with pytest.raises(ExecutionUnknown, match='DISPATCH_RESPONSE_UNCONFIRMED') as raised:
        client.submit(intent)
    assert 'validation rejection' not in str(raised.value)
    metadata, run_id = client._record(intent)
    response = metadata['dispatch_response']
    assert response['http_status'] == 422 and response['github_request_id'] == 'A:1:B:2'
    assert response['body'] == body and run_id is None
    assert 'private authentication' not in json.dumps(metadata)
    assert 'private-link' not in json.dumps(metadata)
    with pytest.raises(ExecutionPending, match='RUN_NOT_YET_OBSERVED'):
        OperatorProductExecution(client.root, gh).submit(intent)
    assert len(calls) == 1


@pytest.mark.parametrize('status,body', [(204, ''), (200, '{"workflow_run_id":1234}')])
def test_dispatch_records_success_response_without_assuming_workflow_success(monkeypatch, status, body):
    monkeypatch.setattr(subprocess, 'run', lambda args, **_kwargs: subprocess.CompletedProcess(
        args, 0, f'HTTP/2.0 {status} Response\r\nX-Github-Request-Id: ABC:123\r\n\r\n{body}'.encode(), b''))
    response = OperatorGitHub().dispatch(42, {})
    assert response['http_status'] == status and response['body'] == body


def test_native_dispatch_receipt_survives_missing_list_entry_and_restart(dispatch):
    client, gh, intent, expected = dispatch
    gh.response = {'http_status': 200, 'body': json.dumps({'workflow_run_id': 1234})}
    gh.runs = []
    gh.run.update(status='in_progress', conclusion=None)
    with pytest.raises(ExecutionPending, match='RUN_IN_PROGRESS'):
        client.submit(intent)
    assert client._record(intent)[1] == '1234'
    gh.run.update(status='completed', conclusion='success')
    restarted = OperatorProductExecution(client.root, gh)
    assert restarted.submit(intent) == restarted.read(intent) == expected
    assert len(gh.posts) == 1


def test_previous_release_http_receipt_is_read_without_a_new_dispatch(dispatch):
    client, gh, intent, expected = dispatch
    gh.runs = []
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    # Previous releases recorded the response but did not bind its run ID.
    metadata, run_id = client._record(intent)
    assert run_id is None
    metadata['dispatch_response'] = {'http_status': 200, 'body': '{"workflow_run_id":1234}'}
    with client._db() as db:
        db.execute('UPDATE executions SET metadata=? WHERE request_id=?', (json.dumps(metadata), intent.request_id))
    assert OperatorProductExecution(client.root, gh).submit(intent) == expected
    assert client._record(intent)[1] == '1234' and len(gh.posts) == 1


@pytest.mark.parametrize('body', ['{}', 'not JSON', '[]', '{"workflow_run_id":true}',
    '{"workflow_run_id":0}', '{"workflow_run_id":-1}', '{"workflow_run_id":"1234"}'])
def test_invalid_dispatch_receipt_cannot_bind_a_run_or_resend(dispatch, body):
    client, gh, intent, _ = dispatch
    gh.response = {'http_status': 200, 'body': body}
    gh.runs = []
    for operation in (client.submit, OperatorProductExecution(client.root, gh).submit):
        with pytest.raises(ExecutionPending, match='RUN_NOT_YET_OBSERVED'):
            operation(intent)
    assert client._record(intent)[1] is None and len(gh.posts) == 1


@pytest.mark.parametrize('key,value', [('actor', {'id': 99}), ('head_sha', 'd' * 40),
    ('workflow_id', 99), ('run_attempt', 2), ('display_title', 'another-request')])
def test_native_run_id_does_not_replace_provider_binding_checks(dispatch, key, value):
    client, gh, intent, _ = dispatch
    gh.response = {'http_status': 200, 'body': json.dumps({'workflow_run_id': 1234})}
    gh.runs = []
    gh.run[key] = value
    with pytest.raises(ExecutionUnknown, match='RUN_BINDING_CONFLICT'):
        client.submit(intent)
    assert client._record(intent)[1] == '1234' and len(gh.posts) == 1


def test_native_receipt_conflicting_with_discovered_run_remains_unknown(dispatch):
    client, gh, intent, _ = dispatch
    def dispatch_with_concurrent_read(workflow_id, inputs):
        gh.posts.append(inputs)
        assert client.read(intent)
        return {'http_status': 200, 'body': json.dumps({'workflow_run_id': 9999})}
    gh.dispatch = dispatch_with_concurrent_read
    with pytest.raises(ExecutionUnknown, match='RUN_BINDING_CONFLICT'):
        client.submit(intent)
    assert client._record(intent)[1] == '1234'
    with pytest.raises(ExecutionUnknown, match='RUN_BINDING_CONFLICT'):
        OperatorProductExecution(client.root, gh).read(intent)
    assert len(gh.posts) == 1


def test_discovery_cannot_overwrite_run_bound_by_concurrent_dispatch_receipt(dispatch):
    client, gh, intent, _ = dispatch
    gh.runs = []
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    gh.runs = [gh.run]
    find = client._find_run
    def find_and_bind(*args):
        run = find(*args)
        with client._db() as db:
            db.execute('UPDATE executions SET run_id=? WHERE request_id=?', ('9999', intent.request_id))
        return run
    client._find_run = find_and_bind
    with pytest.raises(ExecutionUnknown, match='RUN_BINDING_CONFLICT'):
        client.read(intent)
    assert client._record(intent)[1] == '9999' and len(gh.posts) == 1


@pytest.mark.parametrize('mode', ['timeout', 'malformed', 'oversized'])
def test_dispatch_without_trustworthy_http_response_remains_unknown(monkeypatch, mode):
    def call(args, **_kwargs):
        if mode == 'timeout':
            raise subprocess.TimeoutExpired(args, 60, output=b'private partial response')
        raw = b'x' * 65537 if mode == 'oversized' else b'private invalid response'
        return subprocess.CompletedProcess(args, 1, raw, b'private stderr')
    monkeypatch.setattr(subprocess, 'run', call)
    with pytest.raises(DispatchUnconfirmed) as raised:
        OperatorGitHub().dispatch(42, {})
    response = raised.value.response
    assert response['http_status'] is None and response['body'] is None
    assert 'private' not in json.dumps(response)


def test_concurrent_submit_sends_once(dispatch):
    client, gh, intent, expected = dispatch
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(client.submit, [intent] * 4)) == [expected] * 4
    assert len(gh.posts) == 1


def test_missing_run_is_pending_without_new_send(dispatch):
    client, gh, intent, _ = dispatch
    gh.runs = []
    for operation in [client.submit, client.submit, client.read]:
        with pytest.raises(ExecutionPending, match='RUN_NOT_YET_OBSERVED'):
            operation(intent)
    assert len(gh.posts) == 1


def test_intent_conflict_and_unsubmitted_read_do_not_send(dispatch):
    client, gh, intent, _ = dispatch
    with pytest.raises(ExecutionUnknown, match='EXECUTION_NOT_SUBMITTED'):
        client.read(intent)
    client.submit(intent)
    changed = intent.model_copy(update={'input_digest': 'f' * 64})
    with pytest.raises(ExecutionUnknown, match='EXECUTION_INTENT_CONFLICT'):
        client.submit(changed)
    assert len(gh.posts) == 1


@pytest.mark.parametrize('key,value', [('head_sha', 'd' * 40), ('run_attempt', 2), ('workflow_id', 99),
    ('actor', {'id': 99}), ('event', 'push'), ('head_branch', 'main'), ('head_repository', {'full_name': 'elsewhere/repo'})])
def test_other_run_cannot_supply_receipt(dispatch, key, value):
    client, gh, intent, _ = dispatch
    gh.run[key] = value
    with pytest.raises(ExecutionUnknown, match='RUN_BINDING_CONFLICT'):
        client.submit(intent)
    assert len(gh.posts) == 1


def test_multiple_runs_are_unknown_even_after_previous_success(dispatch):
    client, gh, intent, _ = dispatch
    client.submit(intent)
    gh.runs.append(gh.run | {'id': 1235})
    with pytest.raises(ExecutionUnknown, match='MULTIPLE_RUNS_FOR_INTENT'):
        client.read(intent)
    assert len(gh.posts) == 1


@pytest.mark.parametrize('key,value', [('expired', True), ('workflow_run', {'id': 999}),
    ('digest', 'sha256:' + '0' * 64), ('size_in_bytes', 3000000)])
def test_invalid_provider_artifact_cannot_supply_receipt(dispatch, key, value):
    client, gh, intent, _ = dispatch
    gh.artifact[key] = value
    with pytest.raises(ExecutionUnknown):
        client.submit(intent)


def test_archive_never_extracts_traversal_or_trusts_wrong_envelope(dispatch):
    client, gh, intent, receipt = dispatch
    gh.set_artifact(receipt, '../outside.json')
    with pytest.raises(ExecutionUnknown, match='ARTIFACT_RECEIPT_INVALID'):
        client.submit(intent)
    assert not (client.root.parent / 'outside.json').exists()
    gh.set_artifact(receipt | {'github_run_id': '999'})
    with pytest.raises(ExecutionUnknown, match='ARTIFACT_RECEIPT_INVALID'):
        client.read(intent)
    assert len(gh.posts) == 1


def test_pure_contract_import_has_no_runtime_or_network_dependencies():
    code = "import sys; import bravo_agent.product_execution_contracts; assert not ({'boto3','httpx','openai','bravo_agent.coordinator','bravo_agent.subscription_product'} & sys.modules.keys())"
    subprocess.run([sys.executable, '-c', code], check=True)


def test_workflow_pins_source_and_does_not_accept_tokens_or_run_fixtures():
    root = Path(__file__).resolve().parents[3]
    text = (root / '.github/workflows/subscription-product.yml').read_text()
    assert 'ref: ${{ github.workflow_sha }}' in text and 'persist-credentials: false' in text
    assert 'group: bravo-subscription-vm' in text and 'cancel-in-progress: false' in text
    assert text.index('--preflight') < text.index('aws-actions/configure-aws-credentials')
    assert 'controller.py' not in text and '/validation/fixture' not in text
    assert 'web_identity_token' not in text and 'workflow_run:' not in text and 'schedule:' not in text


def test_incomplete_or_failed_run_is_never_reissued(dispatch):
    client, gh, intent, _ = dispatch
    gh.run.update(status='in_progress', conclusion=None)
    with pytest.raises(ExecutionPending, match='RUN_IN_PROGRESS'):
        client.submit(intent)
    gh.run.update(status='completed', conclusion='failure')
    with pytest.raises(ExecutionUnknown, match='RUN_DID_NOT_COMPLETE_SUCCESSFULLY'):
        client.submit(intent)
    assert len(gh.posts) == 1


def test_wrong_current_develop_prevents_any_dispatch(dispatch):
    client, gh, intent, _ = dispatch
    original = gh.get
    gh.get = lambda path: {'object': {'sha': 'e' * 40}} if path.endswith('git/ref/heads/develop') else original(path)
    with pytest.raises(ExecutionUnknown, match='WORKFLOW_OR_SOURCE_NOT_READY'):
        client.submit(intent)
    assert not gh.posts
    with pytest.raises(ExecutionUnknown, match='EXECUTION_NOT_SUBMITTED'):
        client.read(intent)


def test_failed_token_acquisition_opens_no_window_and_persists_no_secret(setup):
    intent = intent_for(setup[2])
    def token():
        raise RuntimeError('private token credential')
    receipt = execute(intent, environment(intent), token_supplier=token,
                      window=lambda *_: pytest.fail('window must not open'))
    assert receipt['status'] == 502 and receipt['cleanup_confirmed'] is False
    assert 'private' not in json.dumps(receipt)


def test_ledger_refuses_symlink(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    target = tmp_path / 'outside'
    target.write_text('untouched')
    (state / 'executions.sqlite3').symlink_to(target)
    with pytest.raises(ValueError):
        OperatorProductExecution(state)
    assert target.read_text() == 'untouched'


def test_preconstructed_invalid_intent_is_revalidated_before_dispatch(dispatch):
    client, gh, intent, _ = dispatch
    with pytest.raises(ValueError):
        client.submit(intent.model_copy(update={'request_id': '../changed'}))
    assert not gh.posts
