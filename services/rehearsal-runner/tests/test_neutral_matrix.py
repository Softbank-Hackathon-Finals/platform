"""3×3: every (source, target) pair goes source policy → neutral → target render, and back.

The render is exported again with the target kind's exporter: the app keeps exactly its access,
the public keeps exactly its read, and the unresolved principal is granted nothing.
"""

import json

import pytest
from neutral_fixtures import (
    APP,
    BUCKET,
    GKE_PRINCIPAL,
    ONPREM_BASE,
    SOURCE_PRINCIPALS,
    SOURCES,
    TARGET_PRINCIPALS,
    TARGET_ROLE,
)

from rehearsal_runner.neutral import (
    KINDS,
    Anonymous,
    Grant,
    Unresolved,
    export_object_policy,
    translate_object_policy,
)

TARGET_BUCKET = "target-attachments"
BACK_PRINCIPALS = {"aws": {TARGET_ROLE: APP}, "gcp": {}, "onprem": {"demo-chat": APP}}
PAIRS = [(s, t) for s in KINDS for t in KINDS]


@pytest.mark.parametrize("source, target", PAIRS, ids=[f"{s}->{t}" for s, t in PAIRS])
def test_source_to_neutral_to_target(source, target):
    result = translate_object_policy(source, target, SOURCES[source], bucket=BUCKET,
                                     principals=SOURCE_PRINCIPALS[source], principal_for=TARGET_PRINCIPALS[target],
                                     target_bucket=TARGET_BUCKET, base=ONPREM_BASE)
    assert result.neutral.source.kind == source and len(result.neutral.unresolved) == 1
    assert result.rendered.kind == target
    assert [s for s in result.rendered.skipped if s.startswith("unresolved:")] == [
        "unresolved:" + result.neutral.unresolved[0]]
    text = result.rendered.text()
    assert "batch" not in text  # the unresolved principal is not granted anywhere
    back = export_object_policy(target, text, bucket=TARGET_BUCKET, principals=BACK_PRINCIPALS[target])
    assert back.bucket == TARGET_BUCKET
    assert set(back.grants) == {Grant(APP, ("read", "write", "list", "delete")), Grant(Anonymous(), ("read",))}
    assert back.public_read and not back.public_list
    assert not any(isinstance(g.principal, Unresolved) for g in back.grants)


@pytest.mark.parametrize("target", KINDS)
def test_a_workload_without_a_target_identity_is_skipped_not_granted(target):
    result = translate_object_policy("aws", target, SOURCES["aws"], bucket=BUCKET, principals=SOURCE_PRINCIPALS["aws"],
                                     principal_for=lambda w: None, target_bucket=TARGET_BUCKET, base={"identities": []})
    assert "no_target_identity:demo-chat/demo-chat" in result.rendered.skipped
    text = result.rendered.text()
    assert TARGET_ROLE not in text and GKE_PRINCIPAL not in text and '"demo-chat"' not in text


def test_renders_are_deterministic_bytes():
    first = translate_object_policy("gcp", "aws", SOURCES["gcp"], bucket=BUCKET, principal_for=TARGET_PRINCIPALS["aws"])
    second = translate_object_policy("gcp", "aws", json.dumps(json.loads(SOURCES["gcp"])), bucket=BUCKET,
                                     principal_for=TARGET_PRINCIPALS["aws"])
    assert first.rendered.text() == second.rendered.text()
    assert first.rendered.digest() == second.rendered.digest()
