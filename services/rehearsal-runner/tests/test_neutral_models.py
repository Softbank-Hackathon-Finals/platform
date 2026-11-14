"""Neutral models match the contract schema and the environment-kinds contract."""

import json
from pathlib import Path

import jsonschema
import pytest

from rehearsal_runner.neutral import (
    KINDS,
    UNSUPPORTED,
    Anonymous,
    Grant,
    NeutralError,
    ObjectAccessPolicy,
    SecretBinding,
    SourceRef,
    Unresolved,
    Workload,
    from_document,
    registry,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "contracts"
SCHEMA = json.loads((CONTRACTS / "neutral-models.schema.json").read_text(encoding="utf-8"))
KINDS_CONTRACT = json.loads((CONTRACTS / "environment-kinds.json").read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)

APP = Workload("demo-chat", "demo-chat")
POLICY = ObjectAccessPolicy(
    "demo-chat-attachments", SourceRef("aws", "demo-chat-attachments"),
    (Grant(Anonymous(), ("read",)), Grant(APP, ("read", "write", "list"), "uploads/"),
     Grant(Unresolved("arn:aws:iam::***:role/batch"), ("read",))),
    public_read=True, public_list=False, tls_required=True)
BINDING = SecretBinding("prod-demo-chat-db", SourceRef("aws", "prod/demo-chat/db"), ("username", "password"),
                        (APP,), rotation_required=True)


def test_constants_follow_the_environment_kinds_contract():
    assert set(KINDS) == set(KINDS_CONTRACT["kinds"])
    assert UNSUPPORTED == KINDS_CONTRACT["unsupported"]
    assert registry.PLAN_REJECTION_PREFIX == KINDS_CONTRACT["plan_rejection_prefix"]
    assert set(registry.SLOTS) <= set(KINDS_CONTRACT["slots"])


@pytest.mark.parametrize("model", [POLICY, BINDING])
def test_documents_validate_against_the_contract_schema_and_round_trip(model):
    doc = model.to_dict()
    VALIDATOR.validate(doc)
    assert from_document(json.loads(json.dumps(doc))) == model
    assert model.digest() == from_document(doc).digest()


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=1),
    lambda d: d["grants"][0]["principal"].update(kind="group"),
    lambda d: d["grants"][1].update(actions=[]),
    lambda d: d["grants"][1].update(actions=["read", "admin"]),
    lambda d: d.update(bucket="Bad_Bucket"),
    lambda d: d["source"].update(kind="azure"),
    lambda d: d["grants"][1]["principal"].update(namespace="Demo"),
    lambda d: d.update(schema="bravo.neutral.object-access/v2"),
])
def test_invalid_documents_are_refused_by_both_the_schema_and_the_model(mutate):
    doc = POLICY.to_dict()
    mutate(doc)
    assert not VALIDATOR.is_valid(doc)
    with pytest.raises(NeutralError):
        from_document(doc)


def test_public_flags_must_agree_with_anonymous_grants():
    doc = POLICY.to_dict()
    doc["public_read"] = False  # schema-valid, but contradicts the anonymous read grant
    assert VALIDATOR.is_valid(doc)
    with pytest.raises(NeutralError, match="PUBLIC_FLAGS_DISAGREE"):
        from_document(doc)


def test_secret_binding_requires_workload_consumers_and_no_values():
    doc = BINDING.to_dict()
    doc["consumers"] = [{"kind": "unresolved", "ref": "x"}]
    assert not VALIDATOR.is_valid(doc)
    with pytest.raises(NeutralError):
        from_document(doc)
