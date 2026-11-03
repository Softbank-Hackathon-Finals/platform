"""시험 항목. ID는 T3 spec의 시험 표와 같다."""

import time
from collections.abc import Callable

from .fixtures import ECHO_PORT, Fixtures
from .kube import Kubectl, ProbeError, Reach
from .model import Kind, Result, Status
from .profile import Target


def _guard(id: str, title: str, kind: Kind, fn: Callable[[], Result]) -> Result:
    """시험 명령 자체가 실패하면 막힘 · 열림을 판단하지 않고 INCONCLUSIVE로 남긴다."""
    try:
        return fn()
    except ProbeError as e:
        return Result(id, title, kind, Status.INCONCLUSIVE, str(e))


def _expect_open(kube: Kubectl, fx: Fixtures, id: str, title: str, host: str, port: int) -> Result:
    def run() -> Result:
        reach = kube.connect(fx.namespace, fx.probe, host, port)
        status = Status.PASS if reach is Reach.OPEN else Status.FAIL
        return Result(id, title, Kind.CONTROL, status, f"{host}:{port} {reach}")

    return _guard(id, title, Kind.CONTROL, run)


def pod_ips(kube: Kubectl, namespace: str, selector: str) -> list[str]:
    pods = kube.get_json("pods", "-n", namespace, "-l", selector)["items"]
    return [p["status"]["podIP"] for p in pods if p.get("status", {}).get("podIP")]


def pods_ready(kube: Kubectl, fx: Fixtures, timeout_s: int = 90) -> Result:
    title = "시험용 Pod가 restricted 조건으로 뜬다"
    pods = [
        (fx.namespace, fx.probe),
        (fx.namespace, fx.echo),
        (fx.victim_namespace, fx.victim),
    ]
    # Pod마다 따로 기다리지 않고 마감 시각 하나를 나눠 쓴다. 이미지가 없을 때 3배로 기다리지 않게.
    deadline = time.monotonic() + timeout_s
    not_ready = [
        f"{ns}/{name}"
        for ns, name in pods
        if not kube.wait_ready(ns, name, max(1, int(deadline - time.monotonic())))
    ]
    if not_ready:
        return Result("P1", title, Kind.CONTROL, Status.FAIL, "준비 안 됨: " + ", ".join(not_ready))
    return Result("P1", title, Kind.CONTROL, Status.PASS)


def controls(kube: Kubectl, fx: Fixtures, targets: list[Target]) -> list[Result]:
    results = [
        _expect_open(kube, fx, "P2", "같은 네임스페이스 서비스에 접속된다", fx.echo, ECHO_PORT),
        _guard("P3", "서비스 이름을 DNS로 찾는다", Kind.CONTROL, lambda: _dns(kube, fx)),
    ]
    for t in targets:
        title = f"허용 대상 {t.name}({t.namespace}, {t.selector}) {t.port}번에 접속된다"
        try:
            ips = pod_ips(kube, t.namespace, t.selector)
        except ProbeError as e:
            results.append(Result("P4", title, Kind.CONTROL, Status.INCONCLUSIVE, str(e)))
            continue
        if not ips:
            results.append(Result("P4", title, Kind.CONTROL, Status.FAIL, "대상 Pod 없음"))
        results += [_expect_open(kube, fx, "P4", title, ip, t.port) for ip in ips]
    return results


def _dns(kube: Kubectl, fx: Fixtures) -> Result:
    # busybox nslookup은 검색 도메인 중 하나라도 실패하면 1을 낸다. 그래서 resolv.conf의 첫 검색 도메인
    # (<네임스페이스>.svc.<클러스터 도메인>)을 붙인 전체 이름으로 묻는다. 클러스터 도메인을 가정하지 않는다.
    title = "서비스 이름을 DNS로 찾는다"
    conf = kube.exec(fx.namespace, fx.probe, "cat", "/etc/resolv.conf")
    domain = first_search_domain(conf.stdout)
    if not domain:
        return Result("P3", title, Kind.CONTROL, Status.INCONCLUSIVE, "resolv.conf에 search 없음")
    name = f"{fx.echo}.{domain}"
    out = kube.exec(fx.namespace, fx.probe, "nslookup", name)
    status = Status.PASS if out.returncode == 0 else Status.FAIL
    return Result("P3", title, Kind.CONTROL, status, name)


def first_search_domain(resolv_conf: str) -> str | None:
    for line in resolv_conf.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[0] == "search":
            return parts[1]
    return None
