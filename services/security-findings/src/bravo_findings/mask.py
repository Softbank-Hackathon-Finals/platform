import json
import re
from typing import Any

# 원본을 저장 · 해시하기 전에 식별자를 가린다. 이미 가린 값에 다시 적용해도 그대로다.
_ACCESS_KEY = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{12}([A-Z0-9]{4})\b")
_ACCOUNT_ID = re.compile(r"(?<![0-9*])[0-9]{8}([0-9]{4})(?![0-9])")
_IPV4 = re.compile(
    r"\b((?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d))\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;\"']+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization|cookie|"
    r"aws_secret_access_key|aws_session_token)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_PASSWORD = re.compile(r"(://[^\s/:@]+:)[^\s/@]+(@)")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|authorization|cookie|api.?key|private.?key|cmdline)"
)


def mask_text(value: str) -> str:
    value = _PRIVATE_KEY.sub("[REDACTED]", value)
    value = _BEARER.sub("[REDACTED_AUTH]", value)
    value = _ASSIGNMENT.sub(lambda m: m.group(1) + "=[REDACTED]", value)
    value = _URL_PASSWORD.sub(r"\1[REDACTED]\2", value)
    value = _ACCESS_KEY.sub(lambda m: f"{m.group(1)}****{m.group(2)}", value)
    value = _IPV4.sub(lambda m: f"{m.group(1)}.*.*", value)
    return _ACCOUNT_ID.sub(lambda m: f"****{m.group(1)}", value)


def mask(raw: Any) -> Any:
    """접근 키 ID는 앞 4자 · 뒤 4자, 12자리 계정 ID는 뒤 4자리, IPv4는 앞 두 자리만 남긴다."""
    if isinstance(raw, dict):
        return {k: "[REDACTED]" if _SENSITIVE_KEY.search(k) else mask(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return [mask(v) for v in raw]
    if isinstance(raw, str):
        return mask_text(raw)
    return raw


# Only source fields consumed by the normalizer can enter the persisted projection.
# Free-form output/message, process arguments, headers, excerpts and unknown fields are omitted.
_PROJECTIONS = {
    "falco": {
        "uuid": None,
        "rule": None,
        "priority": None,
        "time": None,
        "output_fields": {"k8s.pod.name": None, "k8s.ns.name": None},
    },
    "kyverno": {
        "policy": None,
        "rule": None,
        "result": None,
        "severity": None,
        "timestamp": None,
        "resource": {"kind": None, "name": None, "namespace": None},
    },
    "bravo_controls": {
        "run_id": None,
        "finished_at": None,
        "environment": {"id": None, "role": None, "ref": None},
        "result": {
            "test_id": None,
            "control_id": None,
            "verdict": None,
            "unknown_reason": None,
            "evidence": {"collected_at": None},
        },
    },
    "aws_security_hub": {
        "Id": None,
        "Title": None,
        "Types": [None],
        "UpdatedAt": None,
        "CreatedAt": None,
        "FirstObservedAt": None,
        "LastObservedAt": None,
        "Region": None,
        "AwsAccountId": None,
        "ProductName": None,
        "Sample": None,
        "RecordState": None,
        "Workflow": {"Status": None},
        "Severity": {"Label": None},
        "Compliance": {"Status": None, "SecurityControlId": None},
        "Resources": [{"Type": None, "Id": None}],
    },
    "aws_guardduty": {
        "Id": None,
        "Title": None,
        "Type": None,
        "UpdatedAt": None,
        "Region": None,
        "AccountId": None,
        "Severity": None,
        "Service": {"Archived": None},
        "Resource": {"ResourceType": None},
    },
    "gcp_scc": {
        "name": None,
        "category": None,
        "resourceName": None,
        "findingClass": None,
        "createTime": None,
        "eventTime": None,
        "state": None,
        "mute": None,
        "severity": None,
    },
}


def guardduty_sample(raw: dict) -> bool | None:
    """SDK AdditionalInfo.Value is JSON text; retain only a typed sample flag."""
    service = raw.get("Service", {})
    info = service.get("AdditionalInfo", {})
    value = info.get("Value")
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("invalid_guardduty_additional_info")

    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_additional_info_key")
            result[key] = item
        return result

    parsed = json.loads(value, object_pairs_hook=unique_pairs)
    if not isinstance(parsed, dict):
        raise ValueError("invalid_guardduty_additional_info")
    if "sample" in parsed and type(parsed["sample"]) is not bool:
        raise ValueError("invalid_guardduty_sample")
    return parsed.get("sample")


def source_projection(source: str, raw: dict) -> dict:
    def project(value, spec):
        if spec is None:
            if not isinstance(value, str | int | float | bool | type(None)):
                raise ValueError("invalid scalar")
            if isinstance(value, str) and len(value) > 2048:
                raise ValueError("field too long")
            return value
        if isinstance(spec, list):
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError("invalid list")
            return [project(item, spec[0]) for item in value]
        if not isinstance(value, dict):
            raise ValueError("invalid object")
        return {key: project(value[key], child) for key, child in spec.items() if key in value}

    projected = project(raw, _PROJECTIONS[source])
    if source == "aws_guardduty" and (sample := guardduty_sample(raw)) is not None:
        projected.setdefault("Service", {})["AdditionalInfo"] = {
            "Value": json.dumps({"sample": sample}, separators=(",", ":"))
        }
    return mask(projected)
