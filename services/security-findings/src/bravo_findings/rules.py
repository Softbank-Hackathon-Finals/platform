import json
import math
import os
from pathlib import Path
from typing import Any

# 규칙과 스키마의 원본은 docs/security/detection/ 이다. 배포 이미지에는 이 폴더를 함께 넣고
# BRAVO_FINDINGS_RULES_DIR로 위치를 알려 준다.
_REPO_DEFAULT = Path(__file__).resolve().parents[4] / "docs" / "security" / "detection"

_MISSING = object()


def rules_dir() -> Path:
    return Path(os.environ.get("BRAVO_FINDINGS_RULES_DIR", _REPO_DEFAULT))


def get_path(raw: Any, field: str) -> Any:
    """'a.b.c' 경로로 값을 읽는다. 'Types[]'처럼 [] 표기는 목록 값을 그대로 돌려준다."""
    cur = raw
    for part in field.replace("[]", "").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def matches(value: Any, rule: dict) -> bool:
    values = value if isinstance(value, list) else [value]
    for v in values:
        if "equals" in rule and v == rule["equals"]:
            return True
        if "prefix" in rule and isinstance(v, str) and v.startswith(rule["prefix"]):
            return True
    return False


class Rules:
    """mappings.json을 읽어 출처별 값을 정한다. 결정적이고 LLM을 쓰지 않는다."""

    def __init__(self, directory: Path | None = None):
        directory = directory or rules_dir()
        self.mappings = json.loads((directory / "mappings.json").read_text(encoding="utf-8"))
        self.schema = json.loads((directory / "finding.schema.json").read_text(encoding="utf-8"))
        self.ocsf_version = self.mappings["ocsf_version"]

    def sources(self) -> set[str]:
        return set(self.mappings["severity"])

    def severity(self, source: str, raw: dict) -> int:
        spec = self.mappings["severity"][source]
        value = get_path(raw, spec["field"])
        if value is _MISSING or value is None:
            return spec["default"]
        if "ranges" in spec:
            if type(value) not in (int, float) or not math.isfinite(value):
                return spec["default"]
            for band in spec["ranges"]:
                if band["min"] <= value and (
                    value < band["max_exclusive"]
                    or (band.get("max_inclusive") is True and value == band["max_exclusive"])
                ):
                    return band["severity_id"]
            return spec["default"]
        table = spec["map"]
        if spec.get("case_insensitive"):
            table = {k.lower(): v for k, v in table.items()}
            value = str(value).lower()
        return table.get(value, spec["default"])

    def status(self, source: str, raw: dict) -> int:
        spec = self.mappings["status"][source]
        for rule in spec["rules"]:
            value = get_path(raw, rule["field"])
            if value is not _MISSING and matches(value, rule):
                return rule["status_id"]
        return spec["default"]

    def is_dropped(self, source: str, raw: dict) -> bool:
        spec = self.mappings["status"][source]
        return raw.get("result") in spec.get("drop", [])

    def is_collection_error(self, source: str, raw: dict) -> bool:
        spec = self.mappings["status"][source]
        return raw.get("result") in spec.get("collection_error", [])

    def class_uid(self, source: str, raw: dict) -> int:
        return self._first(self.mappings["class"][source], raw, "class_uid")

    def category(self, source: str, raw: dict, class_uid: int) -> tuple[str, bool]:
        """(범주, 규칙으로 정했는지). 규칙이 없으면 클래스 기본값을 쓰고 False를 돌려준다."""
        found = self._first(self.mappings["category"][source], raw, "category")
        if found is not None:
            return found, True
        return self.mappings["category"]["class_fallback"][str(class_uid)], False

    def compliance_status(self, source: str, raw: dict) -> str:
        spec = self.mappings["compliance_status"].get(source)
        if spec is None:
            return "Fail"
        value = get_path(raw, spec["field"])
        return spec["map"].get(value, spec["default"])

    @staticmethod
    def _first(rules: list[dict], raw: dict, key: str) -> Any:
        for rule in rules:
            if "default" in rule:
                return rule["default"]
            value = get_path(raw, rule["field"])
            if value is not _MISSING and matches(value, rule):
                return rule[key]
        return None
