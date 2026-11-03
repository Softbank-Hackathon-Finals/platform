"""Copy a quiescent attachment inventory through bounded object-store adapters.

Adapters expose list_keys(), read(key)->bytes and create(key, bytes). The source
adapter must be read-only; destination create must reject an existing key.
No names, credential values or object bodies are returned as evidence.
"""
import hashlib

MAX_OBJECTS = 100
MAX_BYTES = 16 * 1024 * 1024


def synchronize(source, destination, expected_keys):
    expected = set(expected_keys)
    if not expected or len(expected) > MAX_OBJECTS or any(not isinstance(k, str) or not k for k in expected):
        raise ValueError('bounded nonempty attachment inventory required')
    if set(source.list_keys()) != expected:
        raise ValueError('source bucket and database attachment inventory differ')
    present = set(destination.list_keys())
    if not present <= expected:
        raise ValueError('destination contains unrelated objects; preserve and reconcile')
    hashes = {}
    total = copied = reused = 0
    for key in sorted(expected):
        content = source.read(key)
        if not isinstance(content, bytes) or len(content) > MAX_BYTES:
            raise ValueError('bounded binary source object required')
        total += len(content)
        if total > MAX_BYTES:
            raise ValueError('bounded transfer size exceeded')
        digest = hashlib.sha256(content).hexdigest()
        if key in present:
            if destination.read(key) != content:
                raise ValueError('destination conflict; existing object is preserved')
            reused += 1
        else:
            destination.create(key, content)
            copied += 1
        if destination.read(key) != content:
            raise ValueError('destination byte verification failed')
        if source.read(key) != content:
            raise ValueError('source changed during transfer; require quiescent retry')
        hashes[key] = digest
    if set(source.list_keys()) != expected or set(destination.list_keys()) != expected:
        raise ValueError('final object inventory changed')
    # Bind keys to their content without publishing names. Digest order alone
    # would lose the association between an object name and its bytes.
    import json
    inventory_digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'passed': True, 'source_count': len(expected), 'destination_count': len(expected),
            'copied': copied, 'reused': reused, 'bytes_verified': total,
            'key_content_inventory_sha256': inventory_digest,
            'content_sha256': sorted(hashes.values()), 'source_write_calls': 0}
