import json

import pytest

from bravo_findings.mask import source_projection
from bravo_findings.normalize import Normalizer
from bravo_findings.rules import rules_dir


def raw_finding(value):
    raw = json.loads((rules_dir() / "examples/aws-guardduty-direct.raw.json").read_text())
    raw["Service"]["AdditionalInfo"]["Value"] = value
    return raw


@pytest.mark.parametrize("sample", [True, False])
def test_only_typed_sample_flag_survives_projection(sample):
    raw = raw_finding(json.dumps({"sample": sample, "private_data": "must-not-persist"}))
    projected = source_projection("aws_guardduty", raw)
    assert json.loads(projected["Service"]["AdditionalInfo"]["Value"]) == {"sample": sample}
    assert "must-not-persist" not in json.dumps(projected)
    normalized = Normalizer().normalize(
        "aws_guardduty", raw, environment_id="env-aws", collected_time=1
    )
    assert normalized["bravo"]["sample"] is sample


@pytest.mark.parametrize(
    "value", ["{", "[]", '{"sample":"true"}', '{"sample":true,"sample":false}', "x" * 8193]
)
def test_invalid_sample_metadata_is_rejected(value):
    with pytest.raises(ValueError):
        source_projection("aws_guardduty", raw_finding(value))


def test_unknown_additional_fields_are_not_persisted():
    projected = source_projection("aws_guardduty", raw_finding('{"private_data":"secret"}'))
    assert "AdditionalInfo" not in projected["Service"]
