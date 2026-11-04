"""Operator permissions and revocation for T20 failback (spec commit 6). Synthetic fixtures only."""

import pytest
from test_failback import BINDING, BINDINGS, SPEC, Fixture
from test_operator_access import identity, signing_key  # noqa: F401 - pytest fixtures

from bravo_platform_events.failback import FailbackService
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.store import EventError

# Operator tokens in the shared identity fixture are minted for this time window.
ACCESS_NOW = "2026-11-03T10:00:01+00:00"
GRANT_FROM, GRANT_UNTIL = "2026-11-03T10:00:00+00:00", "2026-11-03T10:10:00+00:00"


class SecuredFailback(Fixture):
    def __init__(self, backend, tokens):
        super().__init__(backend)
        self.now = ACCESS_NOW
        self.access = OperatorAccess(backend.store, clock=lambda: self.now)
        self.operator = tokens.principal()
        self.submitter = tokens.principal("human-2")

    def grant(self, principal=None, *, revision=0, permissions=("read", "approve_failback", "execute_failback")):
        principal = principal or self.operator
        return self.access.set_permissions(SPEC.plan_id, principal, permissions=permissions,
            valid_after=GRANT_FROM, expires_at=GRANT_UNTIL, expected_revision=revision,
            administration_ref="fixture-admin-decision", command_key=f"grant-{revision}-{principal.subject_id}")

    def service(self, **kwargs):
        return FailbackService(self.backend.reopen(), self.client, bindings=BINDINGS,
                               authority=self.access.failback_authority(self.authority),
                               read_integrity=self.read_integrity, edge=self,
                               check_access=self.access.check_read,
                               check_execute=self.access.requires("execute_failback"), clock=lambda: self.now)

    def approve(self, **changes):
        return self.service().approve(self.operator, SPEC.plan_id,
            **(dict(binding=BINDING, expected_version=5, command_key="approve") | changes))

    def submit(self, approval, principal=None):
        return self.service().submit(principal or self.operator, SPEC.plan_id,
                                     approval_id=approval["approval_id"], command_key="submit")


@pytest.fixture
def secured(backend, identity):  # noqa: F811
    f = SecuredFailback(backend, identity)
    yield f
    f.client.close()


def test_cutover_permissions_do_not_cover_failback(secured):
    f = secured
    f.grant(permissions=("read", "approve_cutover", "execute_cutover"))
    with pytest.raises(EventError, match="forbidden"):
        f.approve()
    assert not f.requests


def test_separate_failback_approve_and_execute_rights(secured):
    f = secured
    f.grant(permissions=("read", "approve_failback"))
    f.grant(f.submitter, permissions=("read", "execute_failback"))
    approval = f.approve()
    with pytest.raises(EventError, match="forbidden"):
        f.submit(approval)  # the approver cannot also execute
    assert f.submit(approval, f.submitter)["outcome"] == "OBSERVED"
    assert [r.method for r in f.requests] == ["POST"]


def test_revoked_failback_approval_never_dispatches(secured):
    f = secured
    f.grant()
    approval = f.approve()
    f.access.revoke_approval(SPEC.plan_id, approval["approval_id"], administration_ref="fixture-revoke",
                             approval_kind="failback")
    with pytest.raises(EventError, match="forbidden"):
        f.submit(approval)
    assert not f.requests and f.state() == "WATCH"


def test_revoke_after_dispatch_does_not_claim_remote_cancellation(secured):
    f = secured
    f.grant()
    approval = f.approve()
    f.submit(approval)
    with pytest.raises(EventError, match="approval_already_consumed"):
        f.access.revoke_approval(SPEC.plan_id, approval["approval_id"], administration_ref="fixture-revoke",
                                 approval_kind="failback")
    assert [r.method for r in f.requests] == ["POST"]


def test_reading_is_not_enough_to_move_a_failback_on(secured):
    """Codex #2 review: Edge retry and reconcile are executions, not reads."""
    f = secured
    f.grant()
    f.grant(f.submitter, permissions=("read",))
    f.value.update(status="DONE", committed=True)
    f.edge_result = False
    receipt = f.submit(f.approve())
    assert receipt["failback"] == "edge_pending"
    f.edge_result = True
    service = f.service()
    with pytest.raises(EventError, match="forbidden"):
        service.advance(f.submitter, SPEC.plan_id, receipt["command_id"])
    with pytest.raises(EventError, match="forbidden"):
        service.reconcile(f.submitter, SPEC.plan_id, receipt["command_id"])
    assert len(f.edge_calls) == 1 and f.state() == "FAILBACK"
    assert service.advance(f.operator, SPEC.plan_id, receipt["command_id"]) == "rolled_back"
