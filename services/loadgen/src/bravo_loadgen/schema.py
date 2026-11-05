"""창 요약을 T36 계약 스키마로 검증한다. 스키마의 원본은 docs/observability 하나다."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).resolve().parents[4] / "docs" / "observability" / "sli.schema.json"
PROFILES_PATH = SCHEMA_PATH.with_name("threshold-profiles.json")


def load_profile() -> dict:
    """판정 기준 원본. 값을 여기에 베끼지 않는다."""
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))["profiles"][0]


class SchemaViolation(ValueError):
    """문서가 SliWindow 계약을 어겼다."""


def _window_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    # 스키마 루트는 세 문서 중 하나(oneOf)다. 창 요약만 받도록 SliWindow를 직접 가리킨다.
    # date-time 형식 검사는 FormatChecker를 줘야 동작한다.
    return Draft202012Validator(
        {"$ref": "#/$defs/SliWindow", "$defs": schema["$defs"]},
        format_checker=FormatChecker(),
    )


_VALIDATOR = _window_validator()


def validate_window(document: object) -> None:
    errors = sorted(_VALIDATOR.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise SchemaViolation(f"{where}: {first.message}")
