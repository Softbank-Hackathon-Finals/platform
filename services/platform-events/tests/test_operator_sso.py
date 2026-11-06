"""Operators authenticate with the team's AWS SSO proof, signed for the operator audience only."""

import httpx
import pytest
from test_registration_sso import CONFIG, IDENTITY, proof  # noqa: F401  (fixture)

from bravo_platform_events.operator_identity import (
    OPERATOR_SSO_AUDIENCE,
    OperatorPrincipal,
    OperatorSsoVerifier,
    OperatorTokenVerifier,
)
from bravo_platform_events.registration_sso import (
    AUDIENCE_HEADER,
    SsoRegistrationVerifier,
    make_registration_token,
)


def _sts(request):
    assert request.headers.get(AUDIENCE_HEADER) == OPERATOR_SSO_AUDIENCE  # the verifier signs for operators only
    return httpx.Response(200, json={"GetCallerIdentityResponse": {"GetCallerIdentityResult": IDENTITY}})


def _verifier(clock, audience=OPERATOR_SSO_AUDIENCE):
    identity = SsoRegistrationVerifier(**(CONFIG | {"audience": audience}), clock=lambda: clock[0],
                                       transport=httpx.MockTransport(_sts))
    return OperatorSsoVerifier(identity)


def test_operator_proof_is_an_operator_principal_and_other_audiences_are_not(proof):  # noqa: F811
    verifier = _verifier(proof.clock)
    assert isinstance(verifier, OperatorTokenVerifier)  # accepted by build_trusted_host
    token = make_registration_token(profile_name="test-explicit", region=CONFIG["region"],
                                     audience=OPERATOR_SSO_AUDIENCE)
    actor = verifier.verify(token)
    assert isinstance(actor, OperatorPrincipal) and actor.tenant_id == CONFIG["tenant_id"]
    assert verifier.identity.subject_id == actor.subject_id
    # A registration-audience proof fails at STS (signature over the audience header), as
    # test_registration_sso.test_wrong_signed_audience_or_modified_signature_is_denied covers.


def test_only_the_operator_audience_is_accepted(proof):  # noqa: F811
    with pytest.raises(ValueError):
        _verifier(proof.clock, audience=CONFIG["audience"])
