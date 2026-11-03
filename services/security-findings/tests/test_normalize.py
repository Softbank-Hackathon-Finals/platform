"""정규화기 테스트. 예시 원본 → 정규화 결과를 golden 파일과 비교한다.

golden을 다시 만들려면: BRAVO_REGEN=1 uv run pytest  (바뀐 내용은 diff로 검토한 뒤 커밋한다)
"""

import copy
import json
import os

import pytest

from bravo_findings import CollectionError, Dropped, NormalizationError, Normalizer, Rules
from bravo_findings.normalize import epoch_ms
from bravo_findings.rules import rules_dir

EXAMPLES = rules_dir() / "examples"

# 예시 이름 → (출처, 환경). 수집 시각은 원본 시각 + 1분으로 고정해 결과가 항상 같게 한다.
CASES = {
    "aws-guardduty-via-security-hub": ("aws_security_hub", "env-aws"),
    "aws-security-hub-control": ("aws_security_hub", "env-aws"),
    "aws-guardduty-direct": ("aws_guardduty", "env-aws"),
    "gcp-scc-public-bucket": ("gcp_scc", "env-gcp"),
    "onprem-falco-shell": ("falco", "env-onprem"),
    "onprem-kyverno-fail": ("kyverno", "env-onprem"),
    "controls-iam-01-1-fail": ("bravo_controls", "env-onprem"),
}
TIME_FIELD = {
    "aws_security_hub": "UpdatedAt",
    "aws_guardduty": "UpdatedAt",
    "gcp_scc": "eventTime",
    "falco": "time",
    "kyverno": "timestamp",
}


@pytest.fixture(scope="module")
def normalizer():
    return Normalizer(Rules())


def load_raw(name):
    return json.loads((EXAMPLES / f"{name}.raw.json").read_text(encoding="utf-8"))


def collected_for(source, raw):
    if source == "bravo_controls":
        return epoch_ms(raw["result"]["evidence"]["collected_at"]) + 60_000
    return epoch_ms(raw[TIME_FIELD[source]]) + 60_000


@pytest.mark.parametrize("name", sorted(CASES))
def test_matches_golden(normalizer, name):
    source, env = CASES[name]
    raw = load_raw(name)
    result = normalizer.normalize(
        source, raw, environment_id=env, collected_time=collected_for(source, raw)
    )
    golden = EXAMPLES / f"{name}.normalized.json"
    if os.environ.get("BRAVO_REGEN") == "1":
        golden.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert result == json.loads(golden.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name,expected",
    [
        ("aws-guardduty-via-security-hub", (2004, 4, 1, "THREAT")),
        ("aws-security-hub-control", (2003, 4, 1, "IAM")),
        ("aws-guardduty-direct", (2004, 0, 1, "THREAT")),
        ("gcp-scc-public-bucket", (2003, 4, 1, "EXPOSURE")),
        ("onprem-falco-shell", (2004, 2, 1, "THREAT")),
        ("onprem-kyverno-fail", (2003, 3, 1, "POLICY")),
        ("controls-iam-01-1-fail", (2003, 4, 1, "IAM")),
    ],
)
def test_mapping_rules(normalizer, name, expected):
    """설계 5절 표가 기대하는 값. golden을 다시 만들어도 이 값은 바뀌면 안 된다."""
    source, env = CASES[name]
    raw = load_raw(name)
    f = normalizer.normalize(
        source, raw, environment_id=env, collected_time=collected_for(source, raw)
    )
    assert (f["class_uid"], f["severity_id"], f["status_id"], f["bravo"]["category"]) == expected


def test_deterministic(normalizer):
    raw = load_raw("aws-guardduty-via-security-hub")
    a = normalizer.normalize("aws_security_hub", raw, environment_id="env-aws", collected_time=1)
    b = normalizer.normalize(
        "aws_security_hub", copy.deepcopy(raw), environment_id="env-aws", collected_time=1
    )
    assert a == b


def test_controls_uid_is_stable_across_runs(normalizer):
    """다음 실행의 PASS가 이전 FAIL을 해결 처리하려면 원본 ID가 실행마다 같아야 한다."""
    raw = load_raw("controls-iam-01-1-fail")
    later = copy.deepcopy(raw)
    later["run_id"] = "run-tgt-0002"
    later["result"].update(verdict="PASS", observed="deny")
    first = normalizer.normalize(
        "bravo_controls", raw, environment_id="env-onprem", collected_time=1
    )
    second = normalizer.normalize(
        "bravo_controls", later, environment_id="env-onprem", collected_time=2
    )
    assert first["finding_info"]["uid"] == second["finding_info"]["uid"]
    assert first["finding_info"]["uid"].startswith("sha256:")
    assert (first["status_id"], second["status_id"]) == (1, 4)
    assert second["compliance"]["status"] == "Pass"


def test_controls_unknown_is_warning_and_not_lowered(normalizer):
    raw = load_raw("controls-iam-01-1-fail")
    raw["result"].update(verdict="UNKNOWN", observed=None, unknown_reason="OBSERVATION_DENIED")
    f = normalizer.normalize("bravo_controls", raw, environment_id="env-onprem", collected_time=1)
    assert (f["severity_id"], f["status_id"], f["compliance"]["status"]) == (3, 1, "Warning")
    assert "OBSERVATION_DENIED" in f["finding_info"]["title"]


def test_archived_security_hub_finding_is_resolved(normalizer):
    raw = load_raw("aws-guardduty-via-security-hub")
    raw["RecordState"] = "ARCHIVED"
    f = normalizer.normalize("aws_security_hub", raw, environment_id="env-aws", collected_time=1)
    assert f["status_id"] == 4


def test_muted_scc_finding_is_suppressed(normalizer):
    raw = load_raw("gcp-scc-public-bucket")
    raw["mute"] = "MUTED"
    f = normalizer.normalize("gcp_scc", raw, environment_id="env-gcp", collected_time=1)
    assert f["status_id"] == 3


def test_unmapped_category_falls_back_and_keeps_source_category(normalizer):
    raw = load_raw("gcp-scc-public-bucket")
    raw["category"] = "SOME_NEW_DETECTOR"
    f = normalizer.normalize("gcp_scc", raw, environment_id="env-gcp", collected_time=1)
    assert f["bravo"]["category"] == "POLICY"
    assert f["unmapped"] == {"source_category": "SOME_NEW_DETECTOR"}


def test_kyverno_skip_is_dropped_and_error_is_collection_problem(normalizer):
    raw = load_raw("onprem-kyverno-fail")
    with pytest.raises(Dropped):
        normalizer.normalize(
            "kyverno", {**raw, "result": "skip"}, environment_id="e", collected_time=1
        )
    with pytest.raises(CollectionError):
        normalizer.normalize(
            "kyverno", {**raw, "result": "error"}, environment_id="e", collected_time=1
        )


def test_missing_required_field_is_rejected(normalizer):
    raw = load_raw("aws-security-hub-control")
    del raw["Id"]
    with pytest.raises(NormalizationError, match="Id"):
        normalizer.normalize("aws_security_hub", raw, environment_id="env-aws", collected_time=1)


def test_unknown_source_is_rejected(normalizer):
    with pytest.raises(NormalizationError, match="unknown source"):
        normalizer.normalize("splunk", {}, environment_id="e", collected_time=1)


def test_identifiers_are_masked_before_storage(normalizer):
    raw = load_raw("aws-guardduty-via-security-hub")
    raw["AwsAccountId"] = "123456789012"
    raw["Resources"][0]["Id"] = (
        "arn:aws:iam::123456789012:user/AKIAIOSFODNN7EXAMPLE from 203.0.113.10"
    )
    f = normalizer.normalize("aws_security_hub", raw, environment_id="env-aws", collected_time=1)
    text = json.dumps(f)
    assert (
        "123456789012" not in text
        and "AKIAIOSFODNN7EXAMPLE" not in text
        and "203.0.113.10" not in text
    )
    assert f["cloud"]["account"]["uid"] == "****9012"
