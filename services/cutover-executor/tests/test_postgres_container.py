"""시험용 Postgres를 띄우는 docker run의 실패 처리. docker는 흉내 내고 호출 횟수와 대기만 본다."""

from subprocess import CompletedProcess

import pytest

from tests import conftest

IMAGE = "public.ecr.aws/docker/library/postgres:18.4"
ARGS = ["-d", "--rm", "--name", "cutover-test-1", "-e", "POSTGRES_PASSWORD=fixture-password"]
# 실제 요청 제한에서 받은 문구(2026-11-04 재현)
RATE_LIMITED = "docker: Error response from daemon: error from registry: Rate exceeded"
RATE_LIMITED_HEAD = ('docker: Error response from daemon: unknown: failed to resolve reference "public.ecr.aws/docker/library/'
                     'postgres:18.3": unexpected status from HEAD request to https://public.ecr.aws/v2/docker/library/'
                     "postgres/manifests/18.3: 429 Too Many Requests")
NO_TAG = ('docker: Error response from daemon: failed to resolve reference "public.ecr.aws/docker/library/postgres:0.0": '
          "public.ecr.aws/docker/library/postgres:0.0: not found")
NO_HOST = ('docker: Error response from daemon: failed to resolve reference "registry.invalid/library/postgres:18.4": '
           'failed to do request: Head "https://registry.invalid/v2/library/postgres/manifests/18.4": dial tcp: '
           "lookup registry.invalid: no such host")


@pytest.fixture
def docker(monkeypatch):
    """stderr 목록을 차례로 돌려주고, None이면 성공한다."""
    calls, sleeps = [], []

    def install(*outcomes):
        queue = list(outcomes)

        def run(args, **kwargs):
            calls.append(args)
            error = queue.pop(0)
            return CompletedProcess(args, 0 if error is None else 125, "container-id\n", error or "")

        monkeypatch.setattr(conftest.subprocess, "run", run)
        monkeypatch.setattr(conftest.time, "sleep", sleeps.append)
        return calls, sleeps

    return install


def test_success_does_not_wait(docker):
    calls, sleeps = docker(None)
    conftest.docker_run(ARGS, IMAGE)
    assert len(calls) == 1 and sleeps == []
    assert calls[0] == ["docker", "run", *ARGS, IMAGE]


@pytest.mark.parametrize("error", [RATE_LIMITED, RATE_LIMITED_HEAD, "toomanyrequests: You have reached your pull rate limit",
                                   "net/http: TLS handshake timeout", "read tcp: connection reset by peer", "unexpected EOF"])
def test_transient_error_is_retried_and_reported(docker, error):
    calls, sleeps = docker(error, None)
    with pytest.warns(UserWarning, match="다시 시도") as caught:
        conftest.docker_run(ARGS, IMAGE)
    assert len(calls) == 2 and sleeps == [2]
    assert error in str(caught[0].message) and "fixture-password" not in str(caught[0].message)


def test_retry_is_bounded_and_the_last_error_is_shown(docker):
    calls, sleeps = docker(*[RATE_LIMITED] * 10)
    with pytest.warns(UserWarning), pytest.raises(RuntimeError) as caught:
        conftest.docker_run(ARGS, IMAGE)
    assert len(calls) == 5 and sleeps == [2, 5, 10, 20]
    message = str(caught.value)
    assert RATE_LIMITED in message and "125" in message and IMAGE in message
    assert "fixture-password" not in message


@pytest.mark.parametrize("error", [NO_TAG, NO_HOST, "docker: Error response from daemon: port is already allocated"])
def test_permanent_error_fails_at_once_with_stderr(docker, error):
    calls, sleeps = docker(error)
    with pytest.raises(RuntimeError) as caught:
        conftest.docker_run(ARGS, IMAGE)
    assert len(calls) == 1 and sleeps == []
    assert error in str(caught.value)
