"""k3s 시험 장치의 안전장치. 장치 자체는 Docker가 필요해 CI에서 돌리지 않으므로 여기서 지킨다."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "integration"))

import verify_k3s  # noqa: E402
import verify_real_run  # noqa: E402


@pytest.mark.parametrize("name", ["t27-k3s", "t27-aws", "t27-net"])
def test_guard_accepts_own_names(name):
    assert verify_k3s.guard(name) == name


@pytest.mark.parametrize("name", ["pokeclip-postgres-1", "k3d-t27", "t8-lag", "", "T27-k3s"])
def test_guard_refuses_other_names(name):
    with pytest.raises(ValueError):
        verify_k3s.guard(name)


def test_docker_remove_refuses_before_running_anything(monkeypatch):
    calls = []
    monkeypatch.setattr(verify_k3s, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        verify_k3s.docker_remove("pokeclip-postgres-1")
    assert calls == []


def test_every_resource_name_is_ours():
    for name in (verify_k3s.K3S, verify_k3s.AWS, verify_k3s.SOURCE, verify_k3s.NETWORK):
        assert name.startswith("t27-")


def test_real_run_lab_resources_are_ours():
    for name in (verify_real_run.POOLER_LAB, verify_real_run.POSTGRES_LAB, verify_real_run.PSA_NAMESPACE):
        assert name.startswith("t27-")
