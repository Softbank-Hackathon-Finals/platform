import pytest

from rehearsal_runner.bucket_job import BucketFixture, VerificationUnknown, judge

CONTROL = {"applied": "yes", "authenticated_before": "matches", "anonymous": "allowed",
           "authenticated_after": "matches"}
DENIED = CONTROL | {"anonymous": "denied"}


def test_restricted_candidate_passes_after_open_control():
    assert judge(CONTROL, DENIED) == ("PASS", "NONE")


@pytest.mark.parametrize("candidate,reason", [
    (CONTROL, "ANONYMOUS_READ_ALLOWED"),
    (DENIED | {"applied": "no"}, "POLICY_REJECTED"),
    (DENIED | {"authenticated_after": "failed"}, "AUTHENTICATED_READ_BROKEN"),
    (DENIED | {"authenticated_before": "differs"}, "AUTHENTICATED_READ_BROKEN"),
])
def test_proposal_faults_fail(candidate, reason):
    assert judge(CONTROL, candidate) == ("FAIL", reason)


@pytest.mark.parametrize("control,candidate", [
    (DENIED, DENIED),
    (CONTROL | {"applied": "no"}, DENIED),
    (CONTROL, DENIED | {"anonymous": "unknown"}),
])
def test_inconclusive_environment_is_not_a_verdict(control, candidate):
    with pytest.raises(VerificationUnknown):
        judge(control, candidate)


@pytest.mark.parametrize("change", [
    {"endpoint": "http://host"}, {"bucket": "Bad_Bucket"}, {"canary_sha256": "x"},
    {"image": "amazon/aws-cli:latest"},
])
def test_fixture_requires_bounded_values(change):
    values = dict(endpoint="http://seaweedfs.test.svc:8333", bucket="rehearsal-proof", canary_key="canary",
                  canary_sha256="a" * 64, image="docker.io/amazon/aws-cli@sha256:" + "b" * 64)
    with pytest.raises(ValueError):
        BucketFixture(**(values | change))
