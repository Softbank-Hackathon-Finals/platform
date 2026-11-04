"""Wire validation only: recording an approval never authorizes an effect."""

import json
import re
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = json.loads(files(__package__).joinpath("schemas/event.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
MAX_SEQUENCE = 2**53 - 1


def validate_identifier(value: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid_identifier")


def validate_event(event: dict) -> None:
    VALIDATOR.validate(event)
    if event["event_id"] != f"{event['plan_id']}:{event['sequence']}":
        raise ValueError("event_id_mismatch")
    payload = event["data"]
    if "binding" in payload and payload["binding"]["plan_id"] != event["plan_id"]:
        raise ValueError("binding_plan_mismatch")
    if payload.get("approval_kind") == "failback" and (
            payload["binding"]["source_environment_id"] == payload["binding"]["target_environment_id"]):
        raise ValueError("failback_direction_invalid")


def encode_sse(event: dict) -> str:
    validate_event(event)
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f"id: {event['event_id']}\nevent: {event['event_type']}\ndata: {data}\n\n"
