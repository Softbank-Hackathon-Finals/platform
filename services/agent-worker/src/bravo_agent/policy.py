"""Bounded IAM JSON lint, not a proof of effective permissions or arbitrary code safety."""

import json
import re

_PERMISSIONS = {"Action", "Resource", "Principal", "NotAction", "NotResource", "NotPrincipal"}
_PRINCIPAL_TYPES = {"AWS", "Service", "Federated", "CanonicalUser"}
# Recognition only: text that looks like a policy must not bypass structural parsing.
_POLICY_KEY = re.compile(
    r"\b(?:Statement|Effect|Action|Resource|Principal|NotAction|NotResource|NotPrincipal)"
    r"\b[\"']?\s*[:=]"
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strings(value):
    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(item, str) or not item for item in values):
        raise ValueError("invalid policy permission value")
    return values


def _statement(value):
    if not isinstance(value, dict) or value.get("Effect") not in ("Allow", "Deny"):
        raise ValueError("policy requires an explicit Allow or Deny effect")
    for key in _PERMISSIONS & value.keys():
        permission = value[key]
        if key in {"Principal", "NotPrincipal"} and isinstance(permission, dict):
            if not permission or not permission.keys() <= _PRINCIPAL_TYPES:
                raise ValueError("invalid policy principal")
            values = [item for entry in permission.values() for item in _strings(entry)]
        else:
            values = _strings(permission)
        if value["Effect"] == "Allow":
            if key.startswith("Not") or "*" in values:
                raise ValueError("wildcard or complement permission grant")


def _walk(value, depth):
    if depth > 64:
        raise ValueError("policy nesting limit")
    if isinstance(value, dict):
        if "Statement" in value:
            statements = value["Statement"]
            statements = statements if isinstance(statements, list) else [statements]
            if not statements:
                raise ValueError("empty policy statements")
            for statement in statements:
                _statement(statement)
        if _PERMISSIONS & value.keys() or "Effect" in value:
            _statement(value)
        for child in value.values():
            _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _walk(child, depth + 1)
    elif isinstance(value, str) and (
        _POLICY_KEY.search(value) or value.lstrip().startswith(("{", "["))
    ):
        # CloudFormation and other JSON wrappers can carry a serialized policy.
        _check_json(value, depth + 1)


def _check_json(text, depth):
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError:
        if _POLICY_KEY.search(text) or text.lstrip().startswith(("{", "[")):
            raise ValueError("policy must be unambiguous JSON") from None
        return
    except RecursionError:
        raise ValueError("policy nesting limit") from None
    _walk(value, depth)


def check_policy_permissions(text: str) -> None:
    """Check JSON policies; reject recognizable policies in unsupported source syntax.

    Only an exact, local Effect=Deny exempts a statement from grant checks. Conditions
    and separate Deny statements cannot make an Allow wildcard acceptable here.
    Scoped resource patterns and service action patterns need independent T15 checks.
    """
    _check_json(text, 0)
