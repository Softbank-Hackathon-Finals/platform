import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_subscription_product import setup as setup

from bravo_agent.product_execution_contracts import ExecutionUnknown
from bravo_agent.product_ledger import OperatorLedger
from bravo_agent.subscription_product_seal import open_receipt, seal_receipt


def test_encrypted_artifact_roundtrip_and_fresh_ephemeral_key(dispatch):
    client, _, intent, receipt = dispatch
    first, second = seal_receipt(intent, 1234, receipt), seal_receipt(intent, 1234, receipt)
    assert first['ephemeral_public_key'] != second['ephemeral_public_key']
    assert first['ciphertext'] != second['ciphertext']
    assert open_receipt(intent, 1234, first, client._private_key(intent.request_id)) == receipt
    raw = json.dumps(first)
    assert 'proposal' not in raw and intent.input_ref not in raw and 'test-identity' not in raw
    assert set(first) == {'schema_version', 'recipient_key_digest', 'ephemeral_public_key', 'nonce', 'ciphertext'}


@pytest.mark.parametrize('kind', ['ciphertext', 'nonce', 'ephemeral', 'recipient', 'run', 'admission', 'key'])
def test_other_key_or_modified_ciphertext_or_execution_cannot_decrypt(dispatch, kind):
    client, _, intent, receipt = dispatch
    sealed = seal_receipt(intent, 1234, receipt)
    key, run_id = client._private_key(intent.request_id), 1234
    if kind == 'ciphertext':
        raw = bytearray(base64.b64decode(sealed['ciphertext']))
        raw[-1] ^= 1
        sealed['ciphertext'] = base64.b64encode(raw).decode()
    elif kind == 'nonce':
        sealed['nonce'] = '0' * 24
    elif kind == 'ephemeral':
        sealed['ephemeral_public_key'] = X25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    elif kind == 'recipient':
        sealed['recipient_key_digest'] = '0' * 64
    elif kind == 'run':
        run_id = 999
    elif kind == 'admission':
        intent = intent.model_copy(update={'admission_digest': 'f' * 64})
    else:
        key = X25519PrivateKey.generate()
    with pytest.raises((ValueError, InvalidTag)):
        open_receipt(intent, run_id, sealed, key)


def test_recipient_publication_is_atomic_across_restart_and_concurrent_prepare(tmp_path):
    client = OperatorLedger(tmp_path / 'state')
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(client.recipient, ['once'] * 8))
    assert len(set(keys)) == 1
    assert OperatorLedger(client.root).recipient('once') == keys[0]
    files = list(client.root.glob('recipient-*.key'))
    assert len(files) == 1 and files[0].stat().st_mode & 0o077 == 0
    assert len(files[0].read_bytes()) == 32 and not list(client.root.glob('.recipient-*'))


def test_lost_private_key_after_dispatch_cannot_be_replaced_or_redispatched(dispatch):
    client, gh, intent, _ = dispatch
    client.submit(intent)
    (client.root / ('recipient-' + intent.request_id + '.key')).unlink()
    for operation in [lambda: client.recipient(intent.request_id), lambda: client.submit(intent), lambda: client.read(intent)]:
        with pytest.raises(ExecutionUnknown, match='EXECUTION_KEY_UNAVAILABLE'):
            operation()
    assert len(gh.posts) == 1
    assert not (client.root / ('recipient-' + intent.request_id + '.key')).exists()






def test_wrong_local_recipient_prevents_a_carrier_send(dispatch):
    client, gh, intent, _ = dispatch
    changed = intent.model_copy(update={'recipient_public_key': X25519PrivateKey.generate().public_key().public_bytes_raw().hex()})
    with pytest.raises(ExecutionUnknown, match='EXECUTION_RECIPIENT_CONFLICT'):
        client.submit(changed)
    assert not gh.posts


def test_oversized_plaintext_is_never_sealed(dispatch):
    _, _, intent, _ = dispatch
    with pytest.raises(ValueError, match='oversized product receipt'):
        seal_receipt(intent, 1234, {'private': 'x' * (1024 * 1024)})
