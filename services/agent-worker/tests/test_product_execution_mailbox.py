import copy
import hashlib
import ipaddress
import json
import os
import select
import socket
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_subscription_product import setup as setup

from bravo_agent.product_execution_contracts import ExecutionPending, ExecutionUnknown
from bravo_agent.product_execution_mailbox import FileProductExecution, encode
from bravo_agent.product_mailbox_transfer import upload


@pytest.fixture
def mailbox(dispatch, tmp_path):
    root = tmp_path / 'mailbox'
    root.mkdir(mode=0o700)
    client = FileProductExecution(root)
    client.prepare(dispatch[2])
    return client


def publish(mailbox, intent):
    with pytest.raises(ExecutionPending):
        mailbox.submit(intent)


def test_prepare_is_read_only_until_durable_t10_claim(mailbox, dispatch):
    operator, gh, intent, _ = dispatch
    assert mailbox.recipient(intent.request_id) == intent.recipient_public_key
    assert {p.name for p in mailbox.root.iterdir()} == {'prepared.json'}
    with pytest.raises(ExecutionUnknown, match='NOT_PUBLISHED'):
        mailbox.claimed()
    with pytest.raises(ExecutionUnknown, match='NOT_PUBLISHED'):
        mailbox.read(intent)
    assert gh.posts == []
    publish(mailbox, intent)
    assert mailbox.claimed() == intent
    mailbox.deliver(intent, operator.submit(mailbox.claimed()))
    assert mailbox.read(intent) == dispatch[3]
    assert len(gh.posts) == 1
    for path in mailbox.root.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_restarts_and_duplicate_transfers_preserve_one_request_result(mailbox, dispatch):
    operator, gh, intent, expected = dispatch
    publish(mailbox, intent)
    publish(FileProductExecution(mailbox.root), intent)
    receipt = operator.submit(mailbox.claimed())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: mailbox.deliver(intent, receipt), range(8)))
    restarted = FileProductExecution(mailbox.root)
    restarted.prepare(intent)
    assert restarted.submit(intent) == restarted.read(intent) == expected
    assert len(gh.posts) == 1
    assert not list(mailbox.root.glob('.pending-*'))


@pytest.mark.parametrize('field,value', [('request_id', 'other'), ('input_ref', 'other'),
    ('input_digest', 'e' * 64), ('admission_digest', 'e' * 64), ('runtime_source_sha', 'd' * 40),
    ('mode', 'probe'), ('recipient_public_key', 'a' * 64)])
def test_prepared_binding_cannot_be_replaced_or_submit_other_work(mailbox, dispatch, field, value):
    intent = dispatch[2].model_copy(update={field: value})
    with pytest.raises((ExecutionUnknown, ValueError)):
        mailbox.prepare(intent)
    with pytest.raises(ExecutionUnknown):
        mailbox.submit(intent)
    assert not (mailbox.root / 'intent.json').exists()
    assert dispatch[1].posts == []


def test_recipient_is_local_only_and_wrong_id_fails(mailbox, dispatch):
    with pytest.raises(ExecutionUnknown):
        mailbox.recipient('other')
    before = {p.name: p.read_bytes() for p in mailbox.root.iterdir()}
    assert mailbox.recipient(dispatch[2].request_id) == dispatch[2].recipient_public_key
    assert before == {p.name: p.read_bytes() for p in mailbox.root.iterdir()}


@pytest.mark.parametrize('field,value', [('github_run_attempt', 2), ('cleanup_confirmed', False),
    ('status', True), ('input_digest', 'e' * 64), ('github_run_id', 'wrong'),
    ('admission_digest', 'e' * 64), ('runtime_source_sha', 'd' * 40)])
def test_invalid_receipt_never_creates_or_replaces_result(mailbox, dispatch, field, value):
    intent, receipt = dispatch[2:]
    publish(mailbox, intent)
    with pytest.raises(ExecutionUnknown):
        mailbox.deliver(intent, receipt | {field: value})
    assert not (mailbox.root / 'receipt.json').exists()


def test_receipt_before_claim_and_conflicting_second_receipt_are_refused(mailbox, dispatch):
    intent, receipt = dispatch[2:]
    with pytest.raises(ExecutionUnknown):
        mailbox.deliver(intent, receipt)
    publish(mailbox, intent)
    mailbox.deliver(intent, receipt)
    changed = receipt | {'github_run_id': '9999'}
    with pytest.raises(ExecutionUnknown, match='IMMUTABLE_CONFLICT'):
        mailbox.deliver(intent, changed)
    assert mailbox.read(intent) == receipt


@pytest.mark.parametrize('name', ['prepared.json', 'intent.json', 'receipt.json'])
def test_symlink_cannot_read_or_write_outside(mailbox, dispatch, tmp_path, name):
    intent, receipt = dispatch[2:]
    publish(mailbox, intent)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'untouched')
    path = mailbox.root / name
    path.unlink(missing_ok=True)
    path.symlink_to(outside)
    with pytest.raises((OSError, ExecutionUnknown, ValueError)):
        mailbox.deliver(intent, receipt)
    assert outside.read_bytes() == b'untouched'


@pytest.mark.parametrize('problem', ['world-readable', 'fifo', 'oversized', 'duplicates'])
def test_invalid_file_never_becomes_pending_or_valid(mailbox, dispatch, problem):
    intent = dispatch[2]
    publish(mailbox, intent)
    path = mailbox.root / 'receipt.json'
    if problem == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes({'world-readable': b'{}', 'oversized': b' ' * (1024 * 1024 + 1),
                          'duplicates': b'{"result":{},"result":{}}'}[problem])
        path.chmod(0o644 if problem == 'world-readable' else 0o600)
    with pytest.raises((ExecutionUnknown, ValueError)):
        mailbox.read(intent)


def test_replaced_directory_cannot_supply_another_execution(mailbox, tmp_path, dispatch):
    mailbox.root.rename(tmp_path / 'old')
    mailbox.root.mkdir(mode=0o700)
    with pytest.raises(ExecutionUnknown, match='DIRECTORY_UNCONFIRMED'):
        mailbox.recipient(dispatch[2].request_id)


def tls_material(root, name='receiver', address='127.0.0.1'):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False)
        .sign(key, hashes.SHA256()))
    ca, private = root / (name + '.pem'), root / (name + '.key')
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    private.chmod(0o600)
    return ca, private


def start_receiver(mailbox, raw, *, seconds=10, hash_override=None, address='127.0.0.1'):
    ca, private = tls_material(mailbox.root.parent, address=address)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, '-m', 'bravo_agent.product_mailbox_transfer', 'receive',
        '--directory', str(mailbox.root), '--port', str(port), '--seconds', str(seconds),
        '--sha256', hash_override or hashlib.sha256(raw).hexdigest(), '--bytes', str(len(raw)),
        '--tls-cert', str(ca), '--tls-key', str(private)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready, _, _ = select.select([process.stdout], [], [], 10)
    assert ready
    assert json.loads(process.stdout.readline())['ready'] is True
    return process, port, ca


def finish(process):
    try:
        out, err = process.communicate(timeout=15)
        assert err == ''
        return process.returncode, json.loads(out)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_actual_loopback_receiver_delivers_and_closes(mailbox, dispatch):
    intent, receipt = dispatch[2:]
    publish(mailbox, intent)
    process, port, ca = start_receiver(mailbox, encode(receipt))
    result = upload(port, intent, receipt, ca_file=ca)
    code, observed = finish(process)
    assert code == 0 and observed == result and observed['accepted'] is True
    assert mailbox.read(intent) == receipt
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1', port)) != 0


def test_operator_cli_upload_uses_private_files_and_ignores_proxy(mailbox, dispatch, tmp_path):
    intent, receipt = dispatch[2:]
    publish(mailbox, intent)
    intent_path, receipt_path = tmp_path / 'intent.json', tmp_path / 'receipt.json'
    for path, value in ((intent_path, intent.model_dump()), (receipt_path, receipt)):
        path.write_bytes(encode(value))
        path.chmod(0o600)
    process, port, ca = start_receiver(mailbox, encode(receipt))
    result = subprocess.run([sys.executable, '-m', 'bravo_agent.product_mailbox_transfer',
        'upload', '--intent', str(intent_path), '--receipt', str(receipt_path), '--port', str(port),
        '--ca-file', str(ca)],
        env=os.environ | {'HTTP_PROXY': 'http://127.0.0.1:1', 'ALL_PROXY': 'http://127.0.0.1:1'},
        capture_output=True, text=True, timeout=15)
    code, observed = finish(process)
    assert result.returncode == code == 0 and json.loads(result.stdout) == observed
    assert result.stderr == '' and mailbox.read(intent) == receipt


def test_lost_upload_ack_is_read_back_without_republishing_work(mailbox, dispatch):
    intent, receipt = dispatch[2:]
    publish(mailbox, intent)
    raw = encode(receipt)
    process, port, ca = start_receiver(mailbox, raw)
    context = ssl.create_default_context(cafile=ca)
    with context.wrap_socket(socket.create_connection(('127.0.0.1', port)), server_hostname='127.0.0.1') as sock:
        header = (f'PUT /receipt/{hashlib.sha256(raw).hexdigest()} HTTP/1.1\r\n'
                  f'Host: localhost\r\nContent-Length: {len(raw)}\r\n'
                  'Content-Type: application/json\r\n\r\n').encode()
        sock.sendall(header + raw)
        sock.shutdown(socket.SHUT_WR)
        # The caller deliberately discards any acknowledgement.
    code, observed = finish(process)
    assert code == 0 and observed['accepted'] is True
    assert FileProductExecution(mailbox.root).read(intent) == receipt
    assert mailbox.claimed() == intent and dispatch[1].posts == []


def test_hash_bound_transfer_rejects_changed_body_without_receipt(mailbox, dispatch):
    intent, receipt = dispatch[2:]
    publish(mailbox, intent)
    raw = encode(receipt)
    process, port, ca = start_receiver(mailbox, raw)
    changed = copy.deepcopy(receipt)
    changed['github_run_id'] = '4321'
    with httpx.Client(trust_env=False, verify=ssl.create_default_context(cafile=ca)) as client:
        response = client.put(f'https://127.0.0.1:{port}/receipt/{hashlib.sha256(raw).hexdigest()}',
                              content=encode(changed), headers={'Content-Type': 'application/json'})
    assert response.status_code == 400
    code, observed = finish(process)
    assert code == 1 and observed['redispatch'] is False
    assert not (mailbox.root / 'receipt.json').exists()


def test_slow_headers_obey_hard_deadline_and_leave_no_receipt(mailbox, dispatch):
    publish(mailbox, dispatch[2])
    process, port, ca = start_receiver(mailbox, encode(dispatch[3]), seconds=1)
    started = time.monotonic()
    context = ssl.create_default_context(cafile=ca)
    with context.wrap_socket(socket.create_connection(('127.0.0.1', port)), server_hostname='127.0.0.1') as sock:
        sock.sendall(b'PUT /receipt/ HTTP/1.1\r\nX-Test: ')
        code, observed = finish(process)
    assert time.monotonic() - started < 5
    assert code == 1 and observed['redispatch'] is False
    assert not (mailbox.root / 'receipt.json').exists()


@pytest.mark.parametrize('failure', ['wrong-ca', 'wrong-address'])
def test_untrusted_tls_cannot_receive_receipt(mailbox, dispatch, failure):
    intent, receipt = dispatch[2:]
    publish(mailbox, intent)
    process, port, ca = start_receiver(mailbox, encode(receipt),
        address='192.0.2.1' if failure == 'wrong-address' else '127.0.0.1')
    if failure == 'wrong-ca':
        ca, _ = tls_material(mailbox.root.parent, name='other')
    with pytest.raises(ExecutionUnknown, match='DELIVERY_UNCONFIRMED'):
        upload(port, intent, receipt, ca_file=ca)
    code, _ = finish(process)
    assert code == 1
    assert not (mailbox.root / 'receipt.json').exists()


def test_replaced_plaintext_listener_gets_no_proposal(mailbox, dispatch):
    intent, receipt = dispatch[2:]
    ca, _ = tls_material(mailbox.root.parent)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        def fake_listener():
            connection, _ = listener.accept()
            with connection:
                seen = connection.recv(4096)
                connection.sendall(b'HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n')
            return seen
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(fake_listener)
            with pytest.raises(ExecutionUnknown, match='DELIVERY_UNCONFIRMED'):
                upload(listener.getsockname()[1], intent, receipt, ca_file=ca)
            seen = response.result(timeout=5)
    assert seen.startswith(b'\x16\x03')  # TLS handshake, never HTTP/proposal bytes.
    assert b'PUT ' not in seen and b'proposal' not in seen and encode(receipt) not in seen


def test_receipt_status_cli_never_prints_plaintext_proposal(mailbox, dispatch):
    intent, receipt = dispatch[2:]
    publish(mailbox, intent)
    mailbox.deliver(intent, receipt)
    result = subprocess.run([sys.executable, '-m', 'bravo_agent.product_mailbox_transfer',
                             'receipt-status', '--directory', str(mailbox.root)],
                            capture_output=True, text=True, check=True)
    observed = json.loads(result.stdout)
    assert set(observed) == {'accepted', 'request_id', 'sha256', 'bytes'}
    assert observed['sha256'] == hashlib.sha256(encode(receipt)).hexdigest()
    assert result.stderr == ''
