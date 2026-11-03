import pytest

from rehearsal_runner.isolation.checks import first_search_domain
from rehearsal_runner.isolation.kube import ProbeError, Reach, reach_from
from rehearsal_runner.isolation.profile import read_env, targets


def test_connect_success_is_open():
    assert reach_from(0, "") is Reach.OPEN


def test_nc_failure_inside_pod_is_blocked():
    assert reach_from(1, "command terminated with exit code 1\n") is Reach.BLOCKED


def test_exec_failure_is_not_mistaken_for_blocked():
    # Pod가 없거나 exec가 거부되면 막힘이 아니라 시험 실패다
    with pytest.raises(ProbeError):
        reach_from(1, 'Error from server (NotFound): pods "isolation-probe-x" not found\n')


def test_profile_reads_both_targets(tmp_path):
    path = tmp_path / "p.env"
    path.write_text(
        "# 주석\n"
        "SEAWEEDFS_NAMESPACE=seaweedfs\nSEAWEEDFS_LABEL_KEY=app.kubernetes.io/name\n"
        "SEAWEEDFS_LABEL_VALUE=seaweedfs-s3\nSEAWEEDFS_PORT=8333\n\n"
        "OPENBAO_NAMESPACE=openbao\nOPENBAO_LABEL_KEY=app.kubernetes.io/name\n"
        "OPENBAO_LABEL_VALUE=openbao\nOPENBAO_PORT=8200\n",
        encoding="utf-8",
    )
    s3, bao = targets(read_env(path))
    assert (s3.namespace, s3.selector, s3.port) == ("seaweedfs", "app.kubernetes.io/name=seaweedfs-s3", 8333)
    assert (bao.namespace, bao.port) == ("openbao", 8200)


def test_profile_rejects_line_without_equals(tmp_path):
    path = tmp_path / "p.env"
    path.write_text("SEAWEEDFS_PORT 8333\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_env(path)


def test_first_search_domain_is_namespace_service_domain():
    conf = "search ns1.svc.cluster.local svc.cluster.local cluster.local\nnameserver 10.43.0.10\n"
    assert first_search_domain(conf) == "ns1.svc.cluster.local"
    assert first_search_domain("nameserver 10.43.0.10\n") is None
