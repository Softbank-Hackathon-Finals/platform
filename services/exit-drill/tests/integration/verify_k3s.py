"""실제 k3s에 생성된 NetworkPolicy를 적용해 허용과 거부를 잰다. 수동 실행이며 CI에 넣지 않는다.

    uv run python tests/integration/verify_k3s.py --out <증거 폴더>

만드는 것은 전부 이름이 t27-로 시작하는 컨테이너와 네트워크 하나뿐이고, 끝나면 지운다.
kubectl은 시험 컨테이너 안의 것만 쓰므로 사용자의 kubeconfig를 읽지 않는다. AWS 주소는 Pod의
/etc/hosts로 대역 컨테이너에 묶고, 재기 전에 묶였는지 확인한다. 실제 AWS에는 접속하지 않는다.
"""

import argparse
import copy
import ipaddress
import json
import subprocess
import sys
import time
from pathlib import Path

from bravo_exit_drill import probe as probe_module
from bravo_exit_drill.aws_endpoints import endpoint_host
from bravo_exit_drill.cluster import parse_snapshot
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.observation import build_observation, probe_request
from bravo_exit_drill.policy import PlanRefused, build_plan
from bravo_exit_drill.residual_map import build_map, render_markdown
from bravo_exit_drill.spec import parse_spec
from bravo_exit_drill.verdict import judge

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REPO = ROOT.parents[1]
PROBE_SOURCE = Path(probe_module.__file__).read_text(encoding="utf-8")
STANDIN_SOURCE = (HERE / "standin.py").read_text(encoding="utf-8")

PREFIX = "t27-"
NETWORK = "t27-net"
SUBNET = "10.127.27.0/24"
K3S = "t27-k3s"
AWS = "t27-aws"
SOURCE = "t27-source"
AWS_IP = "10.127.27.10"
SOURCE_IP = "10.127.27.11"
K3S_IP = "10.127.27.12"
K3S_IMAGE = "mirror.gcr.io/rancher/k3s:v1.35.5-k3s1"
PY_IMAGE = "python:3.12-slim"
NODE_PORT = 30432
NS = "target-dev"
TAILNET = "tailscale-system"
COMMON_LABELS = {"component": "target", "app.kubernetes.io/part-of": "bravo-target"}
PROBE_TIMEOUT_S = 3
SETTLE_S = 3
# 앱 Pod에 함께 도는 사이드카. 실제 앱에도 수집기나 Tailscale이 붙을 수 있다.
SIDECAR = "collector"


def guard(name: str) -> str:
    """다른 세션의 자원을 건드리지 않도록 이름을 확인한다."""
    if not name.startswith(PREFIX):
        raise ValueError(f"refusing to touch {name}")
    return name


def run(*args, stdin: str | bytes | None = None, check: bool = True, timeout: int = 180) -> str:
    text = not isinstance(stdin, bytes)
    done = subprocess.run(args, input=stdin, capture_output=True, text=text, timeout=timeout, check=False)
    if check and done.returncode:
        raise RuntimeError(f"{' '.join(map(str, args[:4]))} failed: {done.stderr}")
    return done.stdout if done.returncode == 0 else ""


def docker_remove(*names: str) -> None:
    for name in names:
        run("docker", "rm", "-f", guard(name), check=False)


def kubectl(*args, stdin: str | None = None, check: bool = True, timeout: int = 120) -> str:
    return run("docker", "exec", "-i", K3S, "kubectl", *args, stdin=stdin, check=check, timeout=timeout)


def get_json(*args) -> dict:
    return _strip_noise(json.loads(kubectl("get", *args, "-n", NS, "-o", "json")))


def _strip_noise(doc: dict) -> dict:
    """서버가 덧붙이는 기록만 덜어 낸다. 스펙과 상태는 그대로다."""
    for item in doc.get("items", []):
        metadata = item.get("metadata", {})
        metadata.pop("managedFields", None)
        (metadata.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return doc


def apply(*objects: dict) -> None:
    kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objects)}))


# ---- 시험 환경 ----


def container(name: str, role_env: dict, ip: str) -> None:
    env = [flag for key, value in role_env.items() for flag in ("-e", f"{key}={value}")]
    run("docker", "run", "-d", "--name", guard(name), "--network", NETWORK, "--ip", ip, "--label", "t27=1", *env,
        PY_IMAGE, "python3", "-c", STANDIN_SOURCE)


def start_k3s(*extra: str) -> None:
    run("docker", "run", "-d", "--name", guard(K3S), "--network", NETWORK, "--ip", K3S_IP, "--label", "t27=1", "--privileged",
        "--tmpfs", "/run", "--tmpfs", "/var/run", "-v", f"{HERE / 'k3s-registries.yaml'}:/etc/rancher/k3s/registries.yaml:ro",
        K3S_IMAGE, "server", "--disable", "traefik", "--disable", "servicelb", "--disable", "metrics-server",
        "--disable", "local-storage", *extra)
    wait_until(lambda: " Ready" in kubectl("get", "nodes", check=False), 180, "k3s node")
    architecture = run("docker", "image", "inspect", PY_IMAGE, "--format", "{{.Architecture}}").strip()
    image = subprocess.run(["docker", "save", "--platform", f"linux/{architecture}", PY_IMAGE], capture_output=True, timeout=240, check=True).stdout
    run("docker", "exec", "-i", K3S, "ctr", "-n", "k8s.io", "images", "import", "-", stdin=image, timeout=240)
    wait_until(lambda: "1/1" in kubectl("-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-dns", "--no-headers", check=False), 240, "cluster DNS")


def wait_until(condition, seconds: int, what: str, interval: float = 2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


def standin_container(name: str, env: dict, command=None) -> dict:
    return {
        "name": name,
        "image": PY_IMAGE,
        "imagePullPolicy": "Never",
        "command": command or ["python3", "/standin/standin.py"],
        "env": [{"name": k, "value": str(v)} for k, v in env.items()],
        "volumeMounts": [{"name": "standin", "mountPath": "/standin"}],
    }


def deployment(name: str, namespace: str, labels: dict, env: dict, *, command=None, readiness=None, aliases=None, strategy=None, container="main", sidecars=()) -> dict:
    spec: dict = {
        "containers": [standin_container(container, env, command), *sidecars],
        "volumes": [{"name": "standin", "configMap": {"name": "standin"}}],
        "terminationGracePeriodSeconds": 1,
    }
    if readiness:
        spec["containers"][0]["readinessProbe"] = readiness
    if aliases:
        spec["hostAliases"] = aliases
    body = {"replicas": 1, "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": {**COMMON_LABELS, **labels} if namespace == NS else labels}, "spec": spec}}
    if strategy:
        body["strategy"] = {"type": strategy}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": namespace}, "spec": body}


def service(name: str, namespace: str, selector: dict, port: int, *, target_port: int | None = None, node_port: int | None = None) -> dict:
    entry = {"port": port, "targetPort": target_port or port, "protocol": "TCP"}
    spec: dict = {"selector": selector, "ports": [entry]}
    if node_port:
        spec["type"] = "NodePort"
        entry["nodePort"] = node_port
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def external_name(name: str, target: str) -> dict:
    """실제 환경처럼 target-dev의 이름이 tailnet 네임스페이스의 프록시를 가리키게 한다."""
    return {
        "apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS},
        "spec": {"type": "ExternalName", "externalName": f"{target}.{TAILNET}.svc.cluster.local", "ports": [{"port": 5432, "protocol": "TCP"}]},
    }


def relay(group: str, upstream_host: str, upstream_port: int) -> dict:
    labels = {"tailscale.com/parent-resource": group, "tailscale.com/managed": "true"}
    return deployment(group, TAILNET, labels, {"ROLE": "relay", "LISTEN_PORT": 5432, "UPSTREAM_HOST": upstream_host, "UPSTREAM_PORT": upstream_port})


def aws_aliases(hosts: list[str]) -> list[dict]:
    return [{"ip": AWS_IP, "hostnames": hosts}]


def install_workloads(aws_hosts: list[str]) -> None:
    objects: list[dict] = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}},
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": TAILNET}},
    ]
    for namespace in (NS, TAILNET):
        objects.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "standin", "namespace": namespace}, "data": {"standin.py": STANDIN_SOURCE}})
    aliases = aws_aliases(aws_hosts)
    objects += [
        deployment("demo-chat", NS, {"app": "demo-chat"}, {
            "ROLE": "app", "POOLER_HOST": f"target-pgbouncer.{NS}.svc", "POOLER_PORT": 5432, "HOLD_HOST": aws_hosts[0],
        }, readiness={"httpGet": {"path": "/readyz", "port": 8080}, "periodSeconds": 1}, aliases=aliases, container="app",
            sidecars=[standin_container(SIDECAR, {"ROLE": "tcp", "PORTS": 15090})]),
        service("demo-chat", NS, {"app": "demo-chat"}, 8080),
        deployment("target-pgbouncer", NS, {"app": "target-pgbouncer"}, {"ROLE": "relay", "LISTEN_PORT": 5432, "UPSTREAM_HOST": f"target-db-rw.{NS}.svc", "UPSTREAM_PORT": 5432}, container="target-pgbouncer"),
        service("target-pgbouncer", NS, {"app": "target-pgbouncer"}, 5432),
        deployment("target-db", NS, {"cnpg.io/cluster": "target-db", "cnpg.io/instanceRole": "primary"}, {"ROLE": "db"}, strategy="Recreate", container="postgres"),
        service("target-db-rw", NS, {"cnpg.io/cluster": "target-db", "cnpg.io/instanceRole": "primary"}, 5432),
        deployment("target-seaweedfs", NS, {"app.kubernetes.io/name": "target-seaweedfs"}, {"ROLE": "tcp", "PORTS": 8333}),
        service("seaweedfs", NS, {"app.kubernetes.io/name": "target-seaweedfs"}, 8333),
        deployment("exit-drill-control", NS, {"bravo-exit-drill-control": "true"}, {}, command=["sleep", "86400"], aliases=aliases),
        relay("bravo-target-pooler", SOURCE_IP, 5432),
        service("bravo-target-pooler", TAILNET, {"tailscale.com/parent-resource": "bravo-target-pooler"}, 5432),
        relay("bravo-target-replication", SOURCE_IP, 5432),
        service("bravo-target-replication", TAILNET, {"tailscale.com/parent-resource": "bravo-target-replication"}, 5432),
        relay("target-db-tailnet", f"target-db-rw.{NS}.svc", 5432),
        service("target-db-tailnet", TAILNET, {"tailscale.com/parent-resource": "target-db-tailnet"}, 5432, node_port=NODE_PORT),
        external_name("source-db-for-pooler", "bravo-target-pooler"),
        external_name("source-db-for-replication", "bravo-target-replication"),
    ]
    apply(*objects)
    for namespace, name in [
        (NS, "demo-chat"), (NS, "target-pgbouncer"), (NS, "target-db"), (NS, "target-seaweedfs"), (NS, "exit-drill-control"),
        (TAILNET, "bravo-target-pooler"), (TAILNET, "bravo-target-replication"), (TAILNET, "target-db-tailnet"),
    ]:
        kubectl("-n", namespace, "rollout", "status", f"deployment/{name}", "--timeout=180s", timeout=200)


# ---- 재기 ----


def pod_of(selector: str) -> dict | None:
    """셀렉터에 맞는, 준비된 Pod 하나. 없으면 그 위치에서는 재지 못한다."""
    pods = json.loads(kubectl("get", "pods", "-n", NS, "-l", selector, "-o", "json"))["items"]
    for pod in sorted(pods, key=lambda p: p["metadata"]["creationTimestamp"], reverse=True):
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
        if ready and not pod["metadata"].get("deletionTimestamp"):
            return pod
    return None


def selector_text(plan: dict, vantage: str) -> str:
    return ",".join(f"{k}={v}" for k, v in plan["workloads"][vantage]["selector"].items())


def measure(plan: dict, vantage: str) -> dict | None:
    pod = pod_of(selector_text(plan, vantage))
    if pod is None:
        return None
    request = json.dumps({**probe_request(plan, vantage), "timeout": PROBE_TIMEOUT_S})
    output = kubectl("exec", "-i", "-n", NS, pod["metadata"]["name"], "--", "python3", "-", request, stdin=PROBE_SOURCE, check=False, timeout=180)
    if not output:
        return None
    return {"pod_uid": pod["metadata"]["uid"], "results": json.loads(output)}


def cluster_now(plan: dict) -> str:
    """Pod의 시각을 쓴다. 맥과 Docker VM의 시계가 어긋나도 전후 순서가 틀어지지 않는다."""
    pod = pod_of(selector_text(plan, "control"))
    code = "from datetime import datetime, UTC; print(datetime.now(UTC).isoformat())"
    return kubectl("exec", "-n", NS, pod["metadata"]["name"], "--", "python3", "-c", code).strip()


def read_stream() -> dict:
    code = "import urllib.request as u; print(u.urlopen('http://127.0.0.1:9187/replication', timeout=3).read().decode())"
    return json.loads(run("docker", "exec", SOURCE, "python3", "-c", code))


def stream_samples(plan: dict) -> list[dict]:
    """역방향 복제는 출발지가 접속해 받는다. 그래서 받는 쪽에서 LSN을 읽는다."""
    samples = []
    for _ in range(2):
        state = read_stream()
        samples.append({"route_id": "replication-reverse", "state": state["state"], "lsn": state["lsn"], "observed_at": cluster_now(plan)})
        time.sleep(2)
    return samples


def collect(plan: dict, phase: str, applied_at: str | None, induced_restarts: dict | None = None) -> dict:
    """대조군 → 대상 → 대조군 순서로 재서 거부를 정책 탓으로 돌릴 수 있게 한다."""
    policies_start = get_json("networkpolicies")
    control_first = measure(plan, "control")
    owners = {name: measure(plan, name) for name, w in plan["workloads"].items() if w["role"] in {"app", "replication"}}
    control_last = measure(plan, "control")
    replication = stream_samples(plan)
    policies_end = get_json("networkpolicies")
    runs = {name: run_ for name, run_ in owners.items() if run_}
    runs["control"] = [r for r in (control_first, control_last) if r]
    return build_observation(
        plan, phase=phase, pods=get_json("pods"), policies_start=policies_start, policies_end=policies_end,
        probe_runs=runs, replication=replication,
        cutover={"writer": "target", "note": "stand-in app has always used the stand-in target"},
        policy_applied_at=applied_at,
        induced_restarts=induced_restarts,
    )


def check_no_real_aws(plan: dict, hosts: list[str]) -> dict:
    """앱과 대조군에서 AWS 이름이 실제로 대역 주소로 풀리는지 확인한다. 아니면 재지 않고 멈춘다."""
    code = f"import json,socket; print(json.dumps({{h: socket.gethostbyname(h) for h in {hosts!r}}}))"
    for vantage in ("app", "control"):
        pod = pod_of(selector_text(plan, vantage))
        resolved = json.loads(kubectl("exec", "-n", NS, pod["metadata"]["name"], "--", "python3", "-c", code))
        if set(resolved.values()) != {AWS_IP} or ipaddress.ip_address(AWS_IP) not in ipaddress.ip_network(SUBNET):
            raise RuntimeError("AWS names are not bound to the stand-in; refusing to probe")
    return {"hosts": len(hosts), "bound_to_standin_subnet": True, "vantages": ["app", "control"]}


def restart(*names: str, tolerate_failure: bool = False, timeout: int = 90) -> None:
    for name in names:
        kubectl("-n", NS, "rollout", "restart", f"deployment/{name}")
    for name in names:
        kubectl("-n", NS, "rollout", "status", f"deployment/{name}", f"--timeout={timeout}s", check=not tolerate_failure, timeout=timeout + 15)


def _ready_pods(selector: str) -> list[dict]:
    pods = json.loads(kubectl("get", "pods", "-n", NS, "-l", selector, "-o", "json"))["items"]
    return [
        p for p in pods
        if not p["metadata"].get("deletionTimestamp")
        and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
    ]


def _container_status(pod_name: str, container: str) -> dict:
    status = json.loads(kubectl("get", "pod", pod_name, "-n", NS, "-o", "json"))["status"]
    return next(c for c in status.get("containerStatuses", []) if c["name"] == container)


def in_place_restart(plan: dict, *workloads: str, tolerate_failure: bool = False, signal_instead: dict | None = None) -> dict:
    """Pod는 두고 앱 컨테이너만 다시 띄운다. 이미 정책에 묶인 Pod라 시작 순간부터 차단이 걸려 있다.

    signal_instead는 {워크로드: 컨테이너}로, 앱 대신 다른 컨테이너를 죽이고도 앱을 적는 실수를 재현한다.
    """
    induced = {}
    code = "import os, signal; os.kill(1, signal.SIGTERM)"
    for workload in workloads:
        container = plan["workloads"][workload]["container"]
        victim = (signal_instead or {}).get(workload, container)
        for pod in _ready_pods(selector_text(plan, workload)):
            name = pod["metadata"]["name"]
            replaced = _container_status(name, container)["containerID"]
            count = _container_status(name, victim)["restartCount"]
            kubectl("exec", "-n", NS, name, "-c", victim, "--", "python3", "-c", code, check=False)
            wait_until(lambda n=name, v=victim, c=count: _container_status(n, v)["restartCount"] > c, 30, "container restart")
            # 이번에 시킨 1회만 적는다. 그 전에 죽은 적이 있으면 판정이 UNSTABLE_RESTARTS로 잡는다.
            induced[pod["metadata"]["uid"]] = {"container": container, "replaced_container_id": replaced}
    for workload in workloads:
        selector = selector_text(plan, workload)
        try:
            wait_until(lambda s=selector: bool(_ready_pods(s)), 30, "ready pod after in-place restart")
        except TimeoutError:
            if not tolerate_failure:
                raise
    return induced


def measure_new_pod_window(plan: dict, host: str, trials: int = 3) -> list[dict]:
    """정책이 이미 있을 때 새 Pod의 첫 연결이 막히는지 잰다. Pod 교체를 증거로 쓰면 안 되는 이유다."""
    apply(*copy.deepcopy(plan["manifests"]))
    time.sleep(SETTLE_S)
    code = (
        "import json,socket,time\n"
        "t0=time.monotonic();rows=[]\n"
        "while time.monotonic()-t0<3:\n"
        "    t=time.monotonic()-t0\n"
        f"    try: socket.create_connection(({host!r},443),0.2).close();s='connected'\n"
        "    except OSError: s='denied'\n"
        "    rows.append((t,s));time.sleep(0.05)\n"
        "denied=[t for t,s in rows if s=='denied']\n"
        "print(json.dumps({'first_attempt':rows[0][1],'connected_attempts':sum(s=='connected' for _,s in rows),"
        "'attempts':len(rows),'seconds_until_first_denied':round(denied[0],3) if denied else None}))\n"
    )
    results = []
    for trial in range(trials):
        name = f"{PREFIX}canary-{trial}"
        apply({
            "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS, "labels": {**COMMON_LABELS, "app": "demo-chat"}},
            "spec": {"restartPolicy": "Never", "hostAliases": aws_aliases([host]), "containers": [{"name": "main", "image": PY_IMAGE, "imagePullPolicy": "Never", "command": ["python3", "-c", code]}]},
        })
        kubectl("-n", NS, "wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{name}", "--timeout=60s", timeout=80)
        results.append(json.loads(kubectl("-n", NS, "logs", name)))
        kubectl("-n", NS, "delete", "pod", name, "--ignore-not-found")
    return results


def remove_policies(*names: str) -> None:
    kubectl("-n", NS, "delete", "networkpolicy", "-l", "app.kubernetes.io/part-of=bravo-exit-drill", "--ignore-not-found")
    for name in names:
        kubectl("-n", NS, "delete", "networkpolicy", name, "--ignore-not-found")
    time.sleep(SETTLE_S)


def stream_alive() -> bool:
    return read_stream()["state"] == "streaming"


# ---- 시나리오 ----


def summarize(report: dict) -> dict:
    return {
        "verdict": report["verdict"],
        "counts": report["summary"],
        "not_pass": [{"id": c["id"], "verdict": c["verdict"], "code": c["code"]} for c in report["checks"] if c["verdict"] != "PASS"],
    }


def scenario(plan: dict, *, name: str, meaning: str, expected: str, expected_codes: set[str], apply_plan=True, mutate=None, undo=None, restart_workloads=True, in_place=True, restart_failure_ok: bool = False, on_after=None, signal_instead=None) -> dict:
    """한 바퀴: 차단 전 측정 → 정책 적용 → 망가뜨리기 → Pod 교체 → 컨테이너 재시작 → 측정 → 판정 → 되돌리기."""
    print(f"== {name}", flush=True)
    remove_policies()
    wait_until(stream_alive, 60, "replication stream")
    before = collect(plan, "before", None)
    applied_at = cluster_now(plan)
    if apply_plan:
        apply(*copy.deepcopy(plan["manifests"]))
    time.sleep(SETTLE_S)
    if mutate:
        mutate()
    induced: dict = {}
    if restart_workloads:
        restart("demo-chat", "target-pgbouncer", tolerate_failure=restart_failure_ok, timeout=25 if restart_failure_ok else 90)
        time.sleep(SETTLE_S)  # 새 Pod가 정책에 묶일 시간. 실측은 1초 안쪽이다.
        if in_place:
            induced = in_place_restart(plan, "app", "pooler", tolerate_failure=restart_failure_ok, signal_instead=signal_instead)
            time.sleep(SETTLE_S)  # Service가 다시 뜬 컨테이너로 돌아올 시간
    after = collect(plan, "after", applied_at, induced)
    report = judge(plan, before, after)
    extra = on_after() if on_after else {}
    if undo:
        undo()
    codes = {c["code"] for c in report["checks"]}
    outcome = {
        "name": name,
        "meaning": meaning,
        "expected_verdict": expected,
        "expected_codes": sorted(expected_codes),
        **summarize(report),
        "as_expected": report["verdict"] == expected and expected_codes <= codes,
        "app_container_restarts": [{"app": p["restarts"], "others": p["other_restarts"]} for p in after["pods"]["app"]],
        **extra,
    }
    print(f"   {report['verdict']} (want {expected}) {report['summary']}", flush=True)
    return {"outcome": outcome, "report": report, "before": before, "after": after}


def time_until_blocked(plan: dict, route_host: str) -> dict:
    """적용부터 실제 거부까지 걸린 시간. 앱 Pod의 새 연결로 잰다."""
    pod = pod_of(selector_text(plan, "app"))
    request = json.dumps({"targets": [{"route_id": "x", "kind": "tcp", "host": route_host, "port": 443}], "timeout": 1})
    apply(*copy.deepcopy(plan["manifests"]))
    started = time.monotonic()
    while time.monotonic() - started < 20:
        results = json.loads(kubectl("exec", "-i", "-n", NS, pod["metadata"]["name"], "--", "python3", "-", request, stdin=PROBE_SOURCE))
        if results[0]["state"] != "connected":
            return {"seconds_until_denied": round(time.monotonic() - started, 2), "state": results[0]["state"]}
        time.sleep(0.2)
    return {"seconds_until_denied": None, "state": "connected"}


def read_held(plan: dict) -> bool | None:
    """앱이 시작할 때 연 연결이 살아 있는가. 재시작 직후 Service가 잠깐 안 닿으면 None이다."""
    pod = pod_of(selector_text(plan, "control"))
    code = f"import urllib.request as u; print(u.urlopen('http://demo-chat.{NS}.svc:8080/debug/held', timeout=3).read().decode())"
    output = kubectl("exec", "-n", NS, pod["metadata"]["name"], "--", "python3", "-c", code, check=False)
    return json.loads(output)["alive"] if output else None


def measure_established_connection(plan: dict, host: str) -> dict:
    """정책을 넣어도 이미 맺은 연결이 끊기는지 잰다. 재시작 · 새 연결 시험이 필요한 이유다."""
    remove_policies()
    restart("demo-chat")
    wait_until(lambda: read_held(plan) is True, 30, "held connection")
    timing = time_until_blocked(plan, host)
    time.sleep(5)
    readings: list = []
    wait_until(lambda: readings.append(read_held(plan)) or readings[-1] is not None, 30, "held connection reading")
    return {"held_connection_alive_after_policy": readings[-1], **timing}


def refused_codes(spec_doc: dict, inventory, snapshot) -> list[str]:
    try:
        build_plan(parse_spec(spec_doc), inventory, snapshot)
    except PlanRefused as refused:
        return sorted({v.code for v in refused.violations})
    return []


def source_manifest() -> dict:
    """측정에 쓴 소스를 커밋으로 식별한다. 커밋과 다른 작업 트리로 잰 값을 그 커밋의 증거로 적지 않는다."""
    paths = [str(p.relative_to(REPO)) for p in [*sorted((ROOT / "src/bravo_exit_drill").glob("*.py")), HERE / "standin.py", Path(__file__).resolve()]]
    if run("git", "-C", str(REPO), "status", "--porcelain", "--", *paths).strip():
        raise RuntimeError("commit the measured sources first")
    revision = run("git", "-C", str(REPO), "rev-parse", "HEAD").strip()
    return json.loads(run("python3", str(REPO / "scripts/evidence_sources.py"), "--repo", str(REPO), "create", "--revision", revision, *paths))


def run_all(out: Path) -> int:
    sources = source_manifest()
    inventory_doc = json.loads((ROOT / "tests/fixtures/inventory-demo-chat.json").read_text(encoding="utf-8"))
    spec_doc = json.loads((ROOT / "profiles/target-dev.json").read_text(encoding="utf-8"))
    spec = parse_spec(spec_doc)
    inventory = parse_inventory(inventory_doc, candidate="onprem-demo")
    hosts = sorted({endpoint_host(s, spec.aws_region) for d in inventory.dependencies for s in d.aws_services})

    docker_remove(K3S, AWS, SOURCE)
    run("docker", "network", "rm", guard(NETWORK), check=False)
    run("docker", "network", "create", "--subnet", SUBNET, "--label", "t27=1", guard(NETWORK))
    results: dict = {"scenarios": [], "refusals": {}}
    try:
        container(AWS, {"ROLE": "tcp", "PORTS": "443"}, AWS_IP)
        container(SOURCE, {"ROLE": "source", "REVERSE_HOST": K3S_IP, "REVERSE_PORT": NODE_PORT}, SOURCE_IP)
        start_k3s()
        install_workloads(hosts)
        snapshot = parse_snapshot(get_json("pods,networkpolicies"), namespace=NS)
        plan = build_plan(spec, inventory, snapshot)
        results["aws_binding"] = check_no_real_aws(plan, hosts)
        wait_until(stream_alive, 60, "replication stream")

        # 생성기가 실제 클러스터 읽기로 위험한 입력을 거부하는지 잰다.
        shared = copy.deepcopy(spec_doc)
        shared["workloads"]["app"]["selector"] = {"component": "target"}
        results["refusals"]["selector_shared_with_db"] = refused_codes(shared, inventory, snapshot)

        results["established_connection"] = measure_established_connection(plan, hosts[0])
        print("established:", results["established_connection"], flush=True)
        results["new_pod_window"] = measure_new_pod_window(plan, hosts[0])
        print("new pod window:", results["new_pod_window"], flush=True)

        leak = {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "t27-leak", "namespace": NS},
            "spec": {"podSelector": {"matchLabels": {"app": "demo-chat"}}, "policyTypes": ["Egress"], "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]},
        }
        db_deny = {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "t27-db-deny", "namespace": NS},
            "spec": {"podSelector": {"matchLabels": {"cnpg.io/cluster": "target-db"}}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []},
        }
        overblock = copy.deepcopy(next(m for m in plan["manifests"] if m["metadata"]["name"] == "exit-drill-egress-app"))
        overblock["spec"]["egress"] = [r for r in overblock["spec"]["egress"] if "target-pgbouncer" not in json.dumps(r)]

        def leak_refusal() -> None:
            snap = parse_snapshot(get_json("pods,networkpolicies"), namespace=NS)
            results["refusals"]["additive_allow_leaves_aws_open"] = refused_codes(spec_doc, inventory, snap)

        def apply_leak() -> None:
            apply(leak)
            time.sleep(SETTLE_S)
            leak_refusal()

        def undo_leak() -> None:
            remove_policies("t27-leak")

        def block_db() -> None:
            apply(db_deny)
            restart("target-db")
            time.sleep(SETTLE_S)

        def unblock_db() -> None:
            remove_policies("t27-db-deny")
            wait_until(stream_alive, 60, "replication stream to recover")

        def require_aws() -> None:
            kubectl("-n", NS, "set", "env", "deployment/demo-chat", f"REQUIRE_AT_START={hosts[0]}:443")

        def unrequire_aws() -> None:
            remove_policies()
            kubectl("-n", NS, "set", "env", "deployment/demo-chat", "REQUIRE_AT_START-")
            kubectl("-n", NS, "rollout", "status", "deployment/demo-chat", "--timeout=120s", timeout=140)

        def stop_aws() -> None:
            run("docker", "stop", "-t", "1", guard(AWS))

        def start_aws() -> None:
            run("docker", "start", guard(AWS))
            time.sleep(1)

        def db_http(path: str) -> None:
            pod = pod_of(selector_text(plan, "db"))
            code = f"import urllib.request as u; u.urlopen('http://127.0.0.1:9187{path}', timeout=3).read()"
            kubectl("exec", "-n", NS, pod["metadata"]["name"], "--", "python3", "-c", code)

        def reconnect_under_block() -> dict:
            """정책을 둔 채 DB를 재시작해 복제 연결을 새로 맺어도 흐르는지 잰다."""
            first = read_stream()
            restart("target-db")
            wait_until(stream_alive, 60, "stream after DB restart")
            time.sleep(1)
            second = read_stream()
            return {"stream_reconnected_under_block": second["state"] == "streaming" and first["lsn"] != second["lsn"]}

        passed = scenario(
            plan, name="block_then_restart", meaning="정책을 적용하고 앱과 풀러를 재시작했다. AWS는 닫히고 복제와 앱은 동작해야 한다",
            expected="PASS", expected_codes={"DENIED", "OPEN", "STREAMING_ADVANCED", "RESTARTED_READY", "NEW_CONNECTION_OK", "POLICY_MATCHES_PLAN"},
            on_after=reconnect_under_block,
        )
        results["scenarios"].append(passed["outcome"])
        map_doc = build_map(inventory, plan, passed["report"])
        results["scenarios"].append(scenario(
            plan, name="no_policy", meaning="정책 없이 재시작만 했다. 대조군: 재는 경로가 실제로 열려 있었음을 보인다",
            expected="FAIL", expected_codes={"SOURCE_PATH_OPEN", "POLICY_NOT_APPLIED"}, apply_plan=False,
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="policy_without_restart", meaning="정책만 넣고 재시작하지 않았다. 재시작 시험을 통과한 것으로 치면 안 된다",
            expected="UNKNOWN", expected_codes={"RESTART_NOT_OBSERVED"}, restart_workloads=False,
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="additive_allow_leak", meaning="다른 정책이 외부를 허용해 합집합으로 AWS 경로가 남았다",
            expected="FAIL", expected_codes={"SOURCE_PATH_OPEN", "POLICY_WIDER_THAN_PLAN"}, mutate=apply_leak, undo=undo_leak,
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="replication_blocked", meaning="실수로 DB Pod까지 막았다. 복제 경로 양쪽이 끊겨야 하고 판정이 잡아야 한다",
            expected="FAIL", expected_codes={"REPLICATION_PATH_BROKEN", "REPLICATION_NOT_STREAMING"}, mutate=block_db, undo=unblock_db,
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="local_path_overblocked", meaning="허용 목록에서 풀러를 빼 앱이 DB에 못 닿는다. 준비 신호(/readyz)는 초록으로 남는다",
            expected="FAIL", expected_codes={"LOCAL_PATH_BROKEN", "NEW_CONNECTION_FAILED"}, mutate=lambda: apply(overblock),
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="app_needs_aws_at_start", meaning="시작할 때 AWS가 필요한 앱(고정 소스 ce5727e를 흉내). 정책이 걸린 Pod에서 컨테이너를 다시 띄우면 뜨지 않아야 한다",
            expected="FAIL", expected_codes={"NOT_READY"}, mutate=require_aws, undo=unrequire_aws, restart_failure_ok=True,
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="pod_replacement_only", meaning="같은 앱을 Pod 교체만으로 재시작했다. 새 Pod가 정책에 묶이기 전에 AWS에 닿아 뜰 수 있으므로 통과가 아니다",
            expected="UNKNOWN", expected_codes={"STARTUP_NOT_ISOLATED"}, mutate=require_aws, undo=unrequire_aws, in_place=False, restart_failure_ok=True,
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="sidecar_restarted_instead", meaning="앱 컨테이너 대신 같은 Pod의 사이드카를 다시 띄우고 앱을 적었다. 앱 0회 · 사이드카 1회는 앱이 차단된 채 떴다는 증거가 아니다",
            expected="UNKNOWN", expected_codes={"STARTUP_NOT_ISOLATED"}, signal_instead={"app": SIDECAR},
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="destination_down", meaning="AWS 대역을 꺼서 거부처럼 보이게 했다. 대조군이 닿지 않으니 거부로 세면 안 된다",
            expected="UNKNOWN", expected_codes={"POSITIVE_CONTROL_FAILED"}, mutate=stop_aws, undo=start_aws,
        )["outcome"])
        results["scenarios"].append(scenario(
            plan, name="idle_replication", meaning="쓰기가 없어 LSN이 멈췄다. 채널이 살아 있다고 단정하면 안 된다",
            expected="UNKNOWN", expected_codes={"REPLICATION_IDLE"}, mutate=lambda: db_http("/freeze"), undo=lambda: db_http("/thaw"),
        )["outcome"])

        # 정책 객체가 있어도 CNI가 강제하지 않으면 차단이 아니다(T6에서 EKS 실제로 겪은 일).
        docker_remove(K3S)
        start_k3s("--disable-network-policy")
        install_workloads(hosts)
        check_no_real_aws(plan, hosts)
        results["scenarios"].append(scenario(
            plan, name="cni_does_not_enforce", meaning="NetworkPolicy를 강제하지 않는 클러스터. 정책 객체는 계획과 같지만 AWS가 열려 있다",
            expected="FAIL", expected_codes={"SOURCE_PATH_OPEN", "POLICY_MATCHES_PLAN"},
        )["outcome"])

        results["all_as_expected"] = (
            all(s["as_expected"] for s in results["scenarios"])
            and results["refusals"].get("selector_shared_with_db", []) != []
            and "REPLICATION_OWNER_SELECTED" in results["refusals"]["selector_shared_with_db"]
            and "UNDECLARED_EGRESS" in results["refusals"].get("additive_allow_leaves_aws_open", [])
        )
        results["k3s_image"] = K3S_IMAGE
        results["python_image"] = PY_IMAGE
        results.update(sources)
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        (out / "report-pass.json").write_text(json.dumps(passed["report"], ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        (out / "observation-before.json").write_text(json.dumps(passed["before"], ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        (out / "observation-after.json").write_text(json.dumps(passed["after"], ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        (out / "residual-map.json").write_text(json.dumps(map_doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        (out / "residual-map.md").write_text(render_markdown(map_doc) + "\n", encoding="utf-8")
        return 0 if results["all_as_expected"] else 1
    finally:
        docker_remove(K3S, AWS, SOURCE)
        run("docker", "network", "rm", guard(NETWORK), check=False)
        left = run("docker", "ps", "-a", "--filter", "name=^t27-", "--format", "{{.Names}}", check=False).split()
        print("left containers:", left, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    return run_all(parser.parse_args().out)


if __name__ == "__main__":
    sys.exit(main())
