"""Confidential artifact contents; GitHub provenance still authenticates the sender."""

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .contracts import digest
from .product_execution_contracts import receipt_binding

MAX_PLAINTEXT = 1024 * 1024
_CONTEXT = b'bravo-product-result-v1'


def validate_recipient(public_hex):
    # Reject low-order/null recipient keys before obtaining credentials or running a model.
    key = X25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
    X25519PrivateKey.generate().exchange(key)
    return key


def _derived(shared):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_CONTEXT).derive(shared)


def _aad(binding):
    return json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()


def seal_receipt(intent, run_id, receipt):
    return seal_bound_receipt(intent.recipient_public_key, receipt_binding(intent, run_id), receipt)


def seal_bound_receipt(public_key, binding, receipt):
    """Encrypt against the caller's fixed protocol binding, not request headers."""
    recipient = validate_recipient(public_key)
    ephemeral = X25519PrivateKey.generate()
    raw = json.dumps(receipt, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_PLAINTEXT:
        raise ValueError('oversized product receipt')
    nonce = os.urandom(12)
    sealed = AESGCM(_derived(ephemeral.exchange(recipient))).encrypt(nonce, raw, _aad(binding))
    return {'schema_version': 1, 'recipient_key_digest': digest(public_key),
            'ephemeral_public_key': ephemeral.public_key().public_bytes_raw().hex(), 'nonce': nonce.hex(),
            'ciphertext': base64.b64encode(sealed).decode('ascii')}


def open_receipt(intent, run_id, sealed, private):
    return open_bound_receipt(intent.recipient_public_key, receipt_binding(intent, run_id), sealed, private)


def open_bound_receipt(public_key, binding, sealed, private):
    if (type(sealed) is not dict or set(sealed) != {'schema_version', 'recipient_key_digest', 'ephemeral_public_key', 'nonce', 'ciphertext'}
            or type(sealed['schema_version']) is not int or sealed['schema_version'] != 1
            or sealed['recipient_key_digest'] != digest(public_key)
            or private.public_key().public_bytes_raw().hex() != public_key):
        raise ValueError('unbound encrypted artifact')
    ephemeral = X25519PublicKey.from_public_bytes(bytes.fromhex(sealed['ephemeral_public_key']))
    nonce = bytes.fromhex(sealed['nonce'])
    ciphertext = base64.b64decode(sealed['ciphertext'], validate=True)
    if len(nonce) != 12 or not 16 <= len(ciphertext) <= MAX_PLAINTEXT + 16:
        raise ValueError('invalid encrypted receipt')
    raw = AESGCM(_derived(private.exchange(ephemeral))).decrypt(nonce, ciphertext, _aad(binding))
    return json.loads(raw)
