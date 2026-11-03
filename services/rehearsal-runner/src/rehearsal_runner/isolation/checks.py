"""시험 항목. ID는 T3 spec의 시험 표와 같다."""

import json
import time
from collections.abc import Callable

from .fixtures import ECHO_PORT, Fixtures
from .kube import Kubectl, ProbeError, Reach, reach_from
from .model import Kind, Result, Status
from .profile import Target


def _guard(
    id: str, title: str, kind: Kind, fn: Callable[[], Result], required: bool = True
) -> Result:
    """시험 명령 자체가 실패하면 막힘 · 열림을 판단하지 않고 INCONCLUSIVE로 남긴다."""
    try:
        return fn()
    except ProbeError as e:
        return Result(id, title, kind, Status.INCONCLUSIVE, str(e), required)


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


# ---------------------------------------------------------------------------
# 격리 시험: 반드시 막혀야 하는 시도
# ---------------------------------------------------------------------------

SA_NAME = "rehearsal-exec"
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def _sa(fx: Fixtures) -> str:
    return f"system:serviceaccount:{fx.namespace}:{SA_NAME}"


def _pod_ip(kube: Kubectl, namespace: str, name: str) -> str:
    ip = kube.get_json("pod", name, "-n", namespace).get("status", {}).get("podIP")
    if not ip:
        raise ProbeError(f"{namespace}/{name} Pod IP 없음")
    return ip


def _expect_blocked(
    kube: Kubectl, fx: Fixtures, id: str, title: str, host: str, port: int, required: bool = True
) -> Result:
    """probe에서 막히는지 보고, 정책 없는 victim에서는 열리는지 대조한다.
    victim에서도 닫혀 있으면 대상이 원래 응답하지 않는 것이라 '막혔다'고 말할 수 없다(INCONCLUSIVE)."""

    def run() -> Result:
        target = f"{host}:{port}"
        if kube.connect(fx.namespace, fx.probe, host, port) is Reach.OPEN:
            return Result(id, title, Kind.ISOLATION, Status.FAIL, f"{target} 열림", required)
        if kube.connect(fx.victim_namespace, fx.victim, host, port) is not Reach.OPEN:
            detail = f"{target} 정책 없는 곳에서도 닫힘 (대조 불가)"
            return Result(id, title, Kind.ISOLATION, Status.INCONCLUSIVE, detail, required)
        return Result(id, title, Kind.ISOLATION, Status.PASS, f"{target} 막힘", required)

    return _guard(id, title, Kind.ISOLATION, run, required)


def _ingress_blocked(kube: Kubectl, fx: Fixtures) -> Result:
    title = "다른 네임스페이스에서 리허설 Pod로 들어오지 못한다"

    def run() -> Result:
        ip = _pod_ip(kube, fx.namespace, fx.echo)
        if kube.connect(fx.victim_namespace, fx.victim, ip, ECHO_PORT) is Reach.OPEN:
            return Result("N2", title, Kind.ISOLATION, Status.FAIL, f"victim → {ip} 열림")
        if kube.connect(fx.namespace, fx.probe, ip, ECHO_PORT) is not Reach.OPEN:
            return Result("N2", title, Kind.ISOLATION, Status.INCONCLUSIVE, "echo가 안에서도 닫힘")
        return Result("N2", title, Kind.ISOLATION, Status.PASS, f"victim → {ip}:{ECHO_PORT} 막힘")

    return _guard("N2", title, Kind.ISOLATION, run)


def _no_token(kube: Kubectl, fx: Fixtures) -> Result:
    title = "Pod 안에 서비스 계정 토큰이 없다"

    def run() -> Result:
        out = kube.exec(fx.namespace, fx.probe, "test", "-e", TOKEN_PATH)
        # test가 0이면 파일이 있다. reach_from과 같은 규칙으로 '명령은 돌았고 실패'만 없음으로 본다
        if reach_from(out.returncode, out.stderr) is Reach.OPEN:
            return Result("N6", title, Kind.ISOLATION, Status.FAIL, f"{TOKEN_PATH} 있음")
        return Result("N6", title, Kind.ISOLATION, Status.PASS, f"{TOKEN_PATH} 없음")

    return _guard("N6", title, Kind.ISOLATION, run)


def _admission_denied(
    kube: Kubectl, fx: Fixtures, id: str, title: str, manifest: dict, expect: str
) -> Result:
    """rehearsal-exec 권한으로 서버 dry-run 생성을 시도한다. 거부 사유에 expect가 있어야 통과."""
    out = kube.run(
        "apply", "--dry-run=server", "--as", _sa(fx), "-n", fx.namespace, "-f", "-",
        stdin=json.dumps(manifest),
    )
    if out.returncode == 0:
        return Result(id, title, Kind.ISOLATION, Status.FAIL, "생성이 허용됨")
    reason = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else ""
    status = Status.PASS if expect in out.stderr else Status.INCONCLUSIVE
    return Result(id, title, Kind.ISOLATION, status, reason[:300])


def _privileged_pod(fx: Fixtures) -> dict:
    pod = fx.pod(f"isolation-priv-{fx.run_id}", fx.namespace, "probe", ["sleep", "1"])
    # privileged와 allowPrivilegeEscalation=false는 함께 쓸 수 없어 뺀다
    pod["spec"]["containers"][0]["securityContext"] = {"privileged": True}
    return pod


def _hostpath_pod(fx: Fixtures) -> dict:
    # hostPath 외에는 restricted를 모두 지켜, 거부 사유가 hostPath 하나임을 보인다
    pod = fx.pod(f"isolation-hostpath-{fx.run_id}", fx.namespace, "probe", ["sleep", "1"])
    pod["spec"]["volumes"] = [{"name": "host", "hostPath": {"path": "/"}}]
    pod["spec"]["containers"][0]["volumeMounts"] = [{"name": "host", "mountPath": "/host"}]
    return pod


def _nodeport_service(fx: Fixtures) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"isolation-nodeport-{fx.run_id}", "namespace": fx.namespace},
        "spec": {"type": "NodePort", "selector": {"x": "y"}, "ports": [{"port": 80}]},
    }


def _secret_read_denied(kube: Kubectl, fx: Fixtures) -> Result:
    title = "rehearsal-exec로 다른 네임스페이스 Secret을 읽지 못한다"
    out = kube.run("get", "secrets", "-n", fx.victim_namespace, "--as", _sa(fx))
    if out.returncode == 0:
        return Result("N9", title, Kind.ISOLATION, Status.FAIL, "읽기 허용됨")
    status = Status.PASS if "Forbidden" in out.stderr else Status.INCONCLUSIVE
    return Result("N9", title, Kind.ISOLATION, status, out.stderr.strip()[:300])


# (동사, 자원, 네임스페이스 한정 여부). 하나라도 yes면 권한을 스스로 키우거나 비밀을 볼 수 있다.
ESCALATION_PROBES = [
    ("create", "namespaces", False),
    ("create", "clusterroles", False),
    ("create", "clusterrolebindings", False),
    ("impersonate", "serviceaccounts", False),
    ("create", "roles", True),
    ("create", "rolebindings", True),
    ("escalate", "roles", True),
    ("bind", "roles", True),
    ("get", "secrets", True),
    ("list", "secrets", True),
]


def _no_escalation(kube: Kubectl, fx: Fixtures) -> Result:
    title = "rehearsal-exec로 권한을 키우거나 자기 네임스페이스 Secret도 읽지 못한다"
    allowed, unknown = [], []
    for verb, resource, namespaced in ESCALATION_PROBES:
        args = ["auth", "can-i", verb, resource, "--as", _sa(fx)]
        if namespaced:
            args += ["-n", fx.namespace]
        out = kube.run(*args)
        answer = out.stdout.strip()
        if answer == "yes":
            allowed.append(f"{verb} {resource}")
        elif answer != "no":
            unknown.append(f"{verb} {resource}: {out.stderr.strip()[:80]}")
    if allowed:
        return Result("N10", title, Kind.ISOLATION, Status.FAIL, "허용됨: " + ", ".join(allowed))
    if unknown:
        return Result("N10", title, Kind.ISOLATION, Status.INCONCLUSIVE, "; ".join(unknown))
    return Result("N10", title, Kind.ISOLATION, Status.PASS, f"{len(ESCALATION_PROBES)}개 모두 no")


def _target_side_ports(kube: Kubectl, fx: Fixtures, t: Target) -> list[Result]:
    """허용 대상 네임스페이스에서 허용하지 않은 길: 대상 Pod의 다른 포트(N12), 같은 네임스페이스 다른 Pod(N13).
    Pod에 선언된 containerPort만 시험한다. 선언됐지만 응답하지 않는 포트는 INCONCLUSIVE로 남기고 판정에서 뺀다."""
    results = []
    selector = {t.label_key: t.label_value}
    for pod in kube.get_json("pods", "-n", t.namespace)["items"]:
        ip = pod.get("status", {}).get("podIP")
        if not ip:
            continue
        labels = pod["metadata"].get("labels", {})
        is_target = all(labels.get(k) == v for k, v in selector.items())
        name = pod["metadata"]["name"]
        for c in pod["spec"].get("containers", []):
            for p in c.get("ports", []):
                port = p.get("containerPort")
                if p.get("protocol", "TCP") != "TCP" or (is_target and port == t.port):
                    continue
                if is_target:
                    id, title = "N12", f"허용 대상 {t.namespace}/{name}의 다른 포트 {port}번은 막힌다"
                else:
                    id, title = "N13", f"{t.namespace}의 허용 대상이 아닌 Pod {name}:{port}는 막힌다"
                results.append(_expect_blocked(kube, fx, id, title, ip, port, required=False))
    return results


def isolation(kube: Kubectl, fx: Fixtures, targets: list[Target]) -> list[Result]:
    victim_ip = _pod_ip(kube, fx.victim_namespace, fx.victim)
    results = [
        _expect_blocked(kube, fx, "N1", "다른 네임스페이스로 나가지 못한다", victim_ip, ECHO_PORT),
        _ingress_blocked(kube, fx),
        _expect_blocked(
            kube, fx, "N3", "메타데이터 주소(169.254.169.254)로 나가지 못한다",
            "169.254.169.254", 80, required=False,
        ),
        _expect_blocked(kube, fx, "N4", "인터넷(1.1.1.1)으로 나가지 못한다", "1.1.1.1", 443, required=False),
        _expect_blocked(
            kube, fx, "N4", "인터넷(example.com)으로 나가지 못한다", "example.com", 443, required=False
        ),
        _guard("N5", "쿠버네티스 API 직접 접속 (기록만)", Kind.INFO, lambda: _api_info(kube, fx)),
        _no_token(kube, fx),
        _guard(
            "N7", "privileged Pod는 생성 단계에서 거부된다", Kind.ISOLATION,
            lambda: _admission_denied(
                kube, fx, "N7", "privileged Pod는 생성 단계에서 거부된다",
                _privileged_pod(fx), "privileged",
            ),
        ),
        _guard(
            "N8", "hostPath Pod는 생성 단계에서 거부된다", Kind.ISOLATION,
            lambda: _admission_denied(
                kube, fx, "N8", "hostPath Pod는 생성 단계에서 거부된다",
                _hostpath_pod(fx), "hostPath",
            ),
        ),
        _secret_read_denied(kube, fx),
        _no_escalation(kube, fx),
        _guard(
            "N11", "NodePort 서비스는 할당량으로 거부된다", Kind.ISOLATION,
            lambda: _admission_denied(
                kube, fx, "N11", "NodePort 서비스는 할당량으로 거부된다",
                _nodeport_service(fx), "services.nodeports",
            ),
        ),
    ]
    for t in targets:
        results += _target_side_ports(kube, fx, t)
    return results


def _api_info(kube: Kubectl, fx: Fixtures) -> Result:
    # CNI에 따라 API 서버로 가는 길은 NetworkPolicy로 막히지 않을 수 있다. 토큰이 없는 것(N6)이 1차 방어다.
    title = "쿠버네티스 API 직접 접속 (기록만)"
    reach = kube.connect(fx.namespace, fx.probe, "kubernetes.default.svc", 443)
    status = Status.PASS if reach is Reach.BLOCKED else Status.FAIL
    return Result("N5", title, Kind.INFO, status, f"kubernetes.default.svc:443 {reach}")
