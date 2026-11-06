"""Validate T15 exec messages against the contract v1 schema file (single original)."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "docs/contracts/T42-rehearsal-exec-v1.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_REGISTRY = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))


def errors(definition: str, instance) -> list[str]:
    schema = {"$schema": SCHEMA["$schema"], "$ref": f"{SCHEMA['$id']}#/$defs/{definition}"}
    return [e.message for e in Draft202012Validator(schema, registry=_REGISTRY).iter_errors(instance)]


def assert_valid(definition: str, instance) -> None:
    found = errors(definition, instance)
    assert not found, (definition, found, instance)
