"""T36 계약 스키마. 정본은 docs/observability/sli.schema.json 하나다.

설치된 패키지에는 빌드할 때 같은 파일이 들어간다(pyproject force-include). 소스 체크아웃에서는 저장소 경로를 읽는다.
"""

import json
from functools import cache
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

_REPO_SCHEMA = Path(__file__).resolve().parents[4] / "docs" / "observability" / "sli.schema.json"
# date-time 검사기는 rfc3339-validator가 있어야 등록된다. 없으면 시각 형식을 검사하지 않고 통과시키므로 시작을 거부한다
if "date-time" not in FormatChecker.checkers:
    raise ImportError("rfc3339-validator is required for date-time validation")


class ContractViolation(ValueError):
    """문서가 계약을 어겼다. 메시지에는 위치와 규칙만 담고 값은 담지 않는다."""


@cache
def schema() -> dict:
    packaged = resources.files("bravo_observability").joinpath("sli.schema.json")
    text = packaged.read_text(encoding="utf-8") if packaged.is_file() else _REPO_SCHEMA.read_text(encoding="utf-8")
    return json.loads(text)


@cache
def _validator(definition: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": schema()["$defs"]}, format_checker=FormatChecker()
    )


def validate(definition: str, document: object) -> None:
    """definition: SliWindow · ThresholdProfiles · SliJudgement"""
    errors = sorted(_validator(definition).iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        where = "/".join(str(p) for p in errors[0].absolute_path) or "(root)"
        raise ContractViolation(f"{definition} {where}: {errors[0].validator}")
