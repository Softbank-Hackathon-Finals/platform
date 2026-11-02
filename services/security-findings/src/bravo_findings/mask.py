import re
from typing import Any

# 원본을 저장 · 해시하기 전에 식별자를 가린다. 이미 가린 값에 다시 적용해도 그대로다.
_ACCESS_KEY = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{12}([A-Z0-9]{4})\b")
_ACCOUNT_ID = re.compile(r"(?<![0-9*])[0-9]{8}([0-9]{4})(?![0-9])")
_IPV4 = re.compile(
    r"\b((?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d))\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)


def mask_text(value: str) -> str:
    value = _ACCESS_KEY.sub(lambda m: f"{m.group(1)}****{m.group(2)}", value)
    value = _IPV4.sub(lambda m: f"{m.group(1)}.*.*", value)
    return _ACCOUNT_ID.sub(lambda m: f"****{m.group(1)}", value)


def mask(raw: Any) -> Any:
    """접근 키 ID는 앞 4자 · 뒤 4자, 12자리 계정 ID는 뒤 4자리, IPv4는 앞 두 자리만 남긴다."""
    if isinstance(raw, dict):
        return {k: mask(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return [mask(v) for v in raw]
    if isinstance(raw, str):
        return mask_text(raw)
    return raw
