"""N9·N10은 --as가 실제로 rehearsal-exec로 적용된 경로에서만 판정한다."""

from types import SimpleNamespace

import pytest

from rehearsal_runner.isolation import checks
from rehearsal_runner.isolation.fixtures import Fixtures
from rehearsal_runner.isolation.model import Status

FX = Fixtures("rehearsal-plan1-1", "abc123", "busybox:1.36")
SA = "system:serviceaccount:rehearsal-plan1-1:rehearsal-exec"
PERSON = "person@example.test"


def forbidden(user: str) -> str:
    return f'Error from server (Forbidden): secrets is forbidden: User "{user}" cannot list resource "secrets"'


class Kube:
    """whoami 응답 신원과, 그 신원 기준의 get secrets · can-i 응답을 돌려준다."""

    def __init__(self, identity=SA, whoami_code=0, secrets_stderr=None, can_i="no"):
        self.identity, self.whoami_code = identity, whoami_code
        self.secrets_stderr = forbidden(identity) if secrets_stderr is None else secrets_stderr
        self.can_i = can_i
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append(args[:2])
        if args[:2] == ("auth", "whoami"):
            return SimpleNamespace(returncode=self.whoami_code, stdout=self.identity, stderr="boom")
        if args[:2] == ("get", "secrets"):
            return SimpleNamespace(returncode=1, stdout="", stderr=self.secrets_stderr)
        if args[:2] == ("auth", "can-i"):
            return SimpleNamespace(returncode=0 if self.can_i == "yes" else 1, stdout=self.can_i, stderr="")
        raise AssertionError(args)


def test_denial_of_the_service_account_passes():
    assert checks._secret_read_denied(Kube(), FX).status is Status.PASS
    assert checks._no_escalation(Kube(), FX).status is Status.PASS


def test_ignored_impersonation_is_not_evidence():
    # 인증 프록시가 --as를 버리면 호출한 사람이 거부되거나 사람 권한으로 답이 나온다
    kube = Kube(identity=PERSON, can_i="yes")
    n9, n10 = checks._secret_read_denied(kube, FX), checks._no_escalation(kube, FX)
    assert n9.status is n10.status is Status.INCONCLUSIVE
    assert "가장이 적용되지 않음" in n9.detail
    assert ("get", "secrets") not in kube.calls and ("auth", "can-i") not in kube.calls


def test_unverifiable_impersonation_is_inconclusive():
    kube = Kube(whoami_code=1)
    assert checks._secret_read_denied(kube, FX).status is Status.INCONCLUSIVE
    assert checks._no_escalation(kube, FX).status is Status.INCONCLUSIVE


@pytest.mark.parametrize("stderr", [forbidden(PERSON), "Unable to connect to the server: timeout"])
def test_denial_naming_another_identity_or_no_denial_is_inconclusive(stderr):
    assert checks._secret_read_denied(Kube(secrets_stderr=stderr), FX).status is Status.INCONCLUSIVE


def test_allowed_read_or_escalation_still_fails():
    allowed = Kube()
    allowed.run = lambda *a, **k: (SimpleNamespace(returncode=0, stdout=SA, stderr="")
                                   if a[:2] == ("auth", "whoami") else SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert checks._secret_read_denied(allowed, FX).status is Status.FAIL
    assert checks._no_escalation(Kube(can_i="yes"), FX).status is Status.FAIL
