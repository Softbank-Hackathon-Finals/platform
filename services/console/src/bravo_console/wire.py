"""Bounded JSON objects for authenticated upstream read models.

Authentication does not make ambiguous or resource-exhausting JSON valid.
This decoder is not an authority or a second policy evaluator.
"""

import json
import math


def object_document(raw, *, max_bytes):
    if type(raw) not in (bytes, bytearray) or not 0 < len(raw) <= max_bytes:
        raise ValueError("json_size_limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_field")
            result[key] = value
        return result

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite_json_number")
        return result

    def constant(_):
        raise ValueError("nonfinite_json_number")

    document = json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique, parse_float=finite, parse_constant=constant
    )
    if type(document) is not dict:
        raise ValueError("json_object_required")
    pending, nodes = [(document, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 8192:
            raise ValueError("json_structure_limit")
        if isinstance(item, dict):
            for key, value in item.items():
                key.encode("utf-8")
                pending.append((value, depth + 1))
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
        elif isinstance(item, str):
            item.encode("utf-8")
    return document
