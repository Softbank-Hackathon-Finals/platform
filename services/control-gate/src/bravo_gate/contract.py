"""Bounded immutable JSON and pinned offline T5 contract; no remote references."""

import hashlib
import json
from datetime import UTC, datetime
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.exceptions import NoSuchResource


class GateError(ValueError):
    pass


def time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise GateError("INVALID_TIMESTAMP") from None


def canonical(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GateError("INVALID_JSON") from None


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 1048576:
        raise GateError("BOUNDED_JSON_REQUIRED")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GateError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def invalid(_):
        raise GateError("NONFINITE_JSON")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
        stack = [(value, 0)]
        count = 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > 30000 or depth > 24:
                raise GateError("JSON_COMPLEXITY_LIMIT")
            if isinstance(node, dict):
                stack.extend((v, depth + 1) for v in node.values())
            elif isinstance(node, list):
                stack.extend((v, depth + 1) for v in node)
        canonical(value)  # Includes exponent overflow and lone-surrogate rejection.
        if type(value) is not dict:
            raise GateError("JSON_OBJECT_REQUIRED")
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, GateError):
            raise
        raise GateError("INVALID_JSON") from None


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


class T5Contract:
    def __init__(self):
        root = files("bravo_gate").joinpath("contracts")
        expected = {
            "control-result.schema.json": "e602da1bb2d9573453283ffa41e425d299c3ae73ee1f15663453372faddcf23d",
            "profiles.json": "4ba6e539b422985a43c20875ae00b21e1bd461daf4f8240b5b7b02debe0f2c90",
        }
        docs = {}
        for name, want in expected.items():
            raw = root.joinpath(name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != want:
                raise GateError("PINNED_CONTRACT_CHANGED")
            docs[name] = decode(raw)
        self._schema_raw = canonical(docs["control-result.schema.json"])
        Draft202012Validator.check_schema(self.schema)
        self._catalog_raw = canonical(docs["profiles.json"])
        self.validate(self.catalog, "evaluation_profiles")

    @property
    def schema(self):
        return decode(self._schema_raw)

    @property
    def validator(self):
        return Draft202012Validator(
            self.schema,
            format_checker=FormatChecker(formats=["date-time"]),
            registry=Registry(retrieve=_no_remote),
        )

    @property
    def catalog_digest(self):
        return fingerprint(self.catalog)

    @property
    def catalog(self):
        return decode(self._catalog_raw)

    def validate(self, data, kind):
        if type(data) is not dict or data.get("kind") != kind or not self.validator.is_valid(data):
            raise GateError("SCHEMA_REJECTED")

    def profile(self, profile_id, version):
        matches = [
            p for p in self.catalog["profiles"] if (p["id"], p["version"]) == (profile_id, version)
        ]
        if len(matches) != 1:
            raise GateError("PROFILE_NOT_REGISTERED")
        profile = matches[0]
        ids = profile["required_test_ids"]
        if not ids or len(set(ids)) != len(ids) or any(i not in self.catalog["tests"] for i in ids):
            raise GateError("PROFILE_INTEGRITY")
        return profile
