import hashlib
import json
import math
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator

from bravo_findings.mask import guardduty_sample, source_projection
from bravo_findings.rules import Rules, get_path


class NormalizationError(ValueError):
    """원본에서 필수 값을 찾지 못했거나 결과가 스키마를 어긴다. 수집 상태를 FAILED로 둔다."""


class CollectionError(RuntimeError):
    """출처가 스스로 오류를 보고했다 (예: Kyverno 결과 error). 탐지가 아니라 수집 문제다."""


class SourceNotEnabled(CollectionError):
    """A successful provider read established that detection is not enabled."""


class Dropped(Exception):
    """버리는 원본 (예: Kyverno 결과 skip)."""


def epoch_ms(value: str | int | float) -> int:
    if isinstance(value, bool):
        raise NormalizationError("INVALID_EVENT_TIME")
    if isinstance(value, int | float):
        if not math.isfinite(value):
            raise NormalizationError("INVALID_EVENT_TIME")
        return int(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise NormalizationError("INVALID_EVENT_TIME")
    return int(parsed.timestamp() * 1000)


def _required(raw: dict, field: str, source: str) -> Any:
    value = get_path(raw, field)
    if (
        value is None
        or value == ""
        or not isinstance(value, str | int | float | list | dict | bool)
    ):
        raise NormalizationError(f"{source}: required field missing: {field}")
    return value


class Normalizer:
    """출처별 원본 하나를 정규화된 Finding 하나로 바꾼다. 같은 입력이면 항상 같은 출력이다."""

    def __init__(self, rules: Rules | None = None):
        self.rules = rules or Rules()
        self._validator = Draft202012Validator(
            {"$ref": "#/$defs/Finding", "$defs": self.rules.schema["$defs"]}
        )
        self._extract = {
            "aws_security_hub": self._aws_security_hub,
            "aws_guardduty": self._aws_guardduty,
            "gcp_scc": self._gcp_scc,
            "falco": self._falco,
            "kyverno": self._kyverno,
            "bravo_controls": self._bravo_controls,
        }

    def normalize(
        self, source: str, raw: dict, *, environment_id: str, collected_time: int
    ) -> dict:
        if source not in self._extract:
            raise NormalizationError(f"unknown source: {source}")
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}  # 예시 파일의 주석 키
        if self.rules.is_dropped(source, raw):
            raise Dropped(source)
        if self.rules.is_collection_error(source, raw):
            raise CollectionError(f"{source}: source reported an error")
        if source == "bravo_controls":
            if get_path(raw, "environment.id") != environment_id:
                raise NormalizationError("ENVIRONMENT_MISMATCH")
            if get_path(raw, "result.verdict") not in {"PASS", "FAIL", "UNKNOWN"}:
                raise NormalizationError("INVALID_VERDICT")
        if source == "kyverno" and raw.get("result") not in {"pass", "fail", "warn"}:
            raise NormalizationError("INVALID_VERDICT")
        # Identity is derived before lossy display masking; two accounts must not collapse.
        identity = self._extract[source](raw)["finding_info"]["uid"]
        if not isinstance(identity, str) or not identity or len(identity) > 2048:
            raise NormalizationError("INVALID_IDENTITY")
        try:
            raw = source_projection(source, raw)
        except (ValueError, TypeError, KeyError):
            raise NormalizationError("INVALID_SOURCE_SHAPE") from None

        class_uid = self.rules.class_uid(source, raw)
        category, by_rule = self.rules.category(source, raw, class_uid)
        parts = self._extract[source](raw)

        finding: dict[str, Any] = {
            "class_uid": class_uid,
            "category_uid": 2,
            "activity_id": 1,
            "type_uid": class_uid * 100 + 1,
            "severity_id": self.rules.severity(source, raw),
            "status_id": self.rules.status(source, raw),
            "time": parts.pop("time"),
            "finding_info": parts.pop("finding_info"),
            "metadata": {
                "product": {"name": parts.pop("product")},
                "version": self.rules.ocsf_version,
            },
            "cloud": parts.pop("cloud"),
        }
        finding["finding_info"]["uid"] = (
            "sha256:"
            + hashlib.sha256(
                _canonical({"source": source, "environment": environment_id, "identity": identity})
            ).hexdigest()
        )
        control = parts.pop("control", None)
        if class_uid == 2003:
            finding["compliance"] = {
                "control": control or finding["finding_info"]["title"],
                "status": self.rules.compliance_status(source, raw),
            }
        native_category = parts.pop("native_category", None)
        if not by_rule and native_category:
            finding["unmapped"] = {"source_category": native_category}
        bravo = {
            "normalizer_version": "2",
            "source": source,
            "environment_id": environment_id,
            "category": category,
            "control_ids": parts.pop("control_ids", []),
            "collected_time": collected_time,
            "raw_sha256": "sha256:" + hashlib.sha256(_canonical(raw)).hexdigest(),
        }
        for key in ("native_url", "sample"):
            if (value := parts.pop(key, None)) is not None:
                bravo[key] = value
        finding["bravo"] = bravo
        finding.update({k: v for k, v in parts.items() if v})  # message, resources

        errors = sorted(self._validator.iter_errors(finding), key=lambda e: list(e.path))
        if errors:
            where = "/".join(str(p) for p in errors[0].path)
            raise NormalizationError(f"{source}: invalid normalized field: {where}")
        return finding

    # 출처별 추출 -------------------------------------------------------------

    def _aws_security_hub(self, raw: dict) -> dict:
        region = raw.get("Region")
        info = {
            "uid": _required(raw, "Id", "aws_security_hub"),
            "title": _required(raw, "Title", "aws_security_hub"),
            "types": raw.get("Types", []),
        }
        if first := raw.get("FirstObservedAt") or raw.get("CreatedAt"):
            info["first_seen_time"] = epoch_ms(first)
        if last := raw.get("LastObservedAt") or raw.get("UpdatedAt"):
            info["last_seen_time"] = epoch_ms(last)
        return {
            "time": epoch_ms(_required(raw, "UpdatedAt", "aws_security_hub")),
            "finding_info": info,
            "product": raw.get("ProductName", "Security Hub"),
            "cloud": _cloud("AWS", region, raw.get("AwsAccountId")),
            "resources": [
                {"type": r["Type"], "uid": r["Id"]}
                for r in raw.get("Resources", [])
                if "Type" in r and "Id" in r
            ],
            "control": get_path(raw, "Compliance.SecurityControlId")
            if isinstance(raw.get("Compliance"), dict)
            else None,
            "native_category": (raw.get("Types") or [None])[0],
            "native_url": f"https://console.aws.amazon.com/securityhub/home?region={region}#/findings"
            if region
            else None,
            "sample": raw.get("Sample") is True or None,
        }

    def _aws_guardduty(self, raw: dict) -> dict:
        region = raw.get("Region")
        resource_type = get_path(raw, "Resource.ResourceType")
        return {
            "time": epoch_ms(_required(raw, "UpdatedAt", "aws_guardduty")),
            "finding_info": {
                "uid": _required(raw, "Id", "aws_guardduty"),
                "title": _required(raw, "Title", "aws_guardduty"),
                "types": [raw["Type"]] if raw.get("Type") else [],
            },
            "product": "GuardDuty",
            "cloud": _cloud("AWS", region, raw.get("AccountId")),
            "resources": [{"type": resource_type, "uid": raw["Id"]}]
            if isinstance(resource_type, str)
            else [],
            "native_category": raw.get("Type"),
            "native_url": f"https://console.aws.amazon.com/guardduty/home?region={region}#/findings"
            if region
            else None,
            "sample": guardduty_sample(raw),
        }

    def _gcp_scc(self, raw: dict) -> dict:
        category = _required(raw, "category", "gcp_scc")
        resource = raw.get("resourceName", "")
        info = {
            "uid": _required(raw, "name", "gcp_scc"),
            "title": category,
            "types": [t for t in (raw.get("findingClass"), category) if t],
        }
        if created := raw.get("createTime"):
            info["first_seen_time"] = epoch_ms(created)
        return {
            "time": epoch_ms(_required(raw, "eventTime", "gcp_scc")),
            "finding_info": info,
            "product": "Security Command Center",
            "cloud": {"provider": "GCP"},
            "resources": [{"type": _gcp_resource_type(resource), "uid": resource}]
            if resource
            else [],
            "control": category,
            "native_category": category,
        }

    def _falco(self, raw: dict) -> dict:
        fields = raw.get("output_fields", {})
        pod, ns = fields.get("k8s.pod.name"), fields.get("k8s.ns.name")
        resource = {"type": "k8s.pod", "uid": pod} if pod else None
        if resource and ns:
            resource["namespace"] = ns
        return {
            "time": epoch_ms(_required(raw, "time", "falco")),
            "finding_info": {
                "uid": _required(raw, "uuid", "falco"),
                "title": _required(raw, "rule", "falco"),
            },
            "product": "Falco",
            "cloud": {"provider": "onprem"},
            "resources": [resource] if resource else [],
            "native_category": raw.get("rule"),
        }

    def _kyverno(self, raw: dict) -> dict:
        policy = _required(raw, "policy", "kyverno")
        rule = raw.get("rule", "")
        res = raw.get("resource", {})
        kind, name, ns = res.get("kind", ""), res.get("name", ""), res.get("namespace", "")
        resource = {"type": kind, "uid": name}
        if ns:
            resource["namespace"] = ns
        return {
            "time": epoch_ms(_required(raw, "timestamp", "kyverno")),
            "finding_info": {
                "uid": "/".join([ns, kind, name, policy, rule]),
                "title": f"{policy}: {rule}" if rule else policy,
            },
            "product": "Kyverno",
            "cloud": {"provider": "onprem"},
            "resources": [resource] if kind and name else [],
            "control": policy,
            "native_category": policy,
        }

    def _bravo_controls(self, raw: dict) -> dict:
        """T5 계약(docs/controls)의 TestResult 하나. 원본 ID는 실행이 바뀌어도 같아야 해결 처리가 된다."""
        result = _required(raw, "result", "bravo_controls")
        test_id = _required(result, "test_id", "bravo_controls")
        environment = _required(raw, "environment", "bravo_controls")
        evidence = result.get("evidence") or {}
        collected = evidence.get("collected_at") or raw.get("finished_at")
        if not collected:
            raise NormalizationError("bravo_controls: no evidence.collected_at or finished_at")
        reason = result.get("unknown_reason")
        return {
            "time": epoch_ms(collected),
            "finding_info": {
                "uid": f"{environment['id']}/{test_id}",
                "title": f"{test_id} {result['verdict']}" + (f" ({reason})" if reason else ""),
            },
            "product": "bravo-controls",
            "cloud": {"provider": _provider_from_ref(environment.get("ref", ""))},
            "control": test_id,
            "control_ids": [result["control_id"]],
        }


def _cloud(provider: str, region: str | None, account: str | None) -> dict:
    cloud: dict[str, Any] = {"provider": provider}
    if region:
        cloud["region"] = region
    if account:
        cloud["account"] = {"uid": account}
    return cloud


def _provider_from_ref(ref: str) -> str:
    # T5 ControlRun의 environment.ref: aws:eks/source, gcp:..., onprem:rke2/target
    return {"aws": "AWS", "gcp": "GCP"}.get(ref.split(":", 1)[0], "onprem")


def _gcp_resource_type(resource_name: str) -> str:
    # //storage.googleapis.com/bucket → storage.googleapis.com
    if resource_name.startswith("//"):
        return resource_name[2:].split("/", 1)[0]
    return "gcp.resource"


def _canonical(raw: dict) -> bytes:
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
