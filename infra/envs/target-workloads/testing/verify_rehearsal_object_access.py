#!/usr/bin/env python3
"""리허설 Pod → SeaweedFS 8333 NetworkPolicy를 임시 k3s에서 잰다. 수동 실행이며 CI에 넣지 않는다.

    uv run --no-project --python 3.12 python infra/envs/target-workloads/testing/verify_rehearsal_object_access.py --out <폴더>

만드는 것은 t30rh-k3s 컨테이너 하나이고 끝나면 지운다. kubeconfig는 임시 폴더의 것만 쓰며 사용자의 설정을
읽거나 바꾸지 않는다. 실제 클러스터·VM에는 닿지 않는다. 결과에는 Pod 주소를 남기지 않는다.

실제: objects.py가 렌더한 Service·NetworkPolicy·라벨, T3 namespace 템플릿과 격리 게이트, T15 Job의 Pod 라벨, k3s 집행기.
대역: SeaweedFS·OpenBao 프로세스. 같은 라벨과 포트로 TCP만 받으므로 S3 인증과 버킷 정책은 재지 않는다.
"""
import argparse
import collections
import copy
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "services/rehearsal-runner"
sys.path[:0] = [str(ROOT / "infra/envs/target-workloads"), str(RUNNER / "src")]

import objects
from rehearsal_runner import namespace as t3_namespace
from rehearsal_runner.bucket_job import BucketFixture, SeaweedBucketJob
from rehearsal_runner.isolation.fixtures import Fixtures
from rehearsal_runner.isolation.profile import read_env

K3S = "t30rh-k3s"
K3S_IMAGE = "mirror.gcr.io/rancher/k3s:v1.35.5-k3s1"
PY_IMAGE = "python:3.12-slim"
T3_IMAGE = "docker.io/library/busybox:1.36"
REGISTRIES = ROOT / "services/exit-drill/tests/integration/k3s-registries.yaml"
PROFILE = RUNNER / "profiles/onprem.env"
SEAWEED_HOST = "seaweedfs.target-dev.svc.cluster.local"
OTHER_PORT = 8334

BARE, FULL, NO_LABEL, OTHER, TARGET = "rehearsal-t30bare", "rehearsal-t30full", "rehearsal-t30nolabel", "t30-other", "target-dev"
GATE = "rehearsal-t30gate"

LISTENER = """
import os, socket, threading, time
def serve(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port)); s.listen(64)
    while True:
        s.accept()[0].close()
for port in os.environ["PORTS"].split(","):
    threading.Thread(target=serve, args=(int(port),), daemon=True).start()
time.sleep(10**8)
"""
PROBE = """
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 3).close(); print("OPEN")
except socket.gaierror: print("DNS")
except TimeoutError: print("TIMEOUT")
except ConnectionRefusedError: print("REFUSED")
except OSError as error: print("ERRNO_" + str(error.errno))
"""
# 정책 때문에 닫힌 것으로 인정하는 응답. DNS 오류나 exec 실패는 막힘으로 세지 않는다. 113 = EHOSTUNREACH.
BLOCKED_SHAPES = {"TIMEOUT", "REFUSED", "ERRNO_113"}


def t15_pod_labels():
    fixture = BucketFixture(f"http://{SEAWEED_HOST}:8333", "rehearsal-attachments", "canary", "0" * 64,
                            "registry.test/aws@sha256:" + "0" * 64)
    job = SeaweedBucketJob(None, None, None, fixture, None)._job(None, "bucket-control", "source.json", {"namespace": GATE})
    return job["spec"]["template"]["metadata"]["labels"]


def t3_pod_labels(role):
    return Fixtures(GATE, "t30", T3_IMAGE).pod("pod", GATE, role, ["true"])["metadata"]["labels"]


VERIFIER, T3_PROBE, T3_ECHO = t15_pod_labels(), t3_pod_labels("probe"), t3_pod_labels("echo")
OBJECT_CLIENT = {"bravo-object-client": "true"}
CLIENTS = {
    "bare-verifier": (BARE, VERIFIER), "bare-probe": (BARE, T3_PROBE), "bare-nolabel": (BARE, {}),
    "bare-echo": (BARE, T3_ECHO), "bare-objectclient": (BARE, OBJECT_CLIENT),
    "full-verifier": (FULL, VERIFIER), "full-probe": (FULL, T3_PROBE), "full-nolabel": (FULL, {}),
    "nolabel-verifier": (NO_LABEL, VERIFIER), "nolabel-probe": (NO_LABEL, T3_PROBE),
    "other-verifier": (OTHER, VERIFIER),
    "target-objectclient": (TARGET, OBJECT_CLIENT), "target-verifier": (TARGET, VERIFIER),
    "target-probe": (TARGET, T3_PROBE), "target-nolabel": (TARGET, {}),
}


def on_8333(*names):
    return {(name, 8333) for name in names}


# 정책 없이 듣는지 보는 대조. full-* 의 다른 포트는 T3 송신 정책이 막는다.
LISTENER_CONTROL = {(c, p) for c in CLIENTS for p in (8333, OTHER_PORT) if not (c.startswith("full-") and p == OTHER_PORT)}
RENDERED = on_8333("bare-verifier", "bare-probe", "full-verifier", "full-probe", "target-objectclient")
ALL_REHEARSAL_PODS = on_8333("bare-nolabel", "bare-echo", "bare-objectclient", "full-nolabel")


def run(*args, stdin=None, check=True, timeout=180):
    text = not isinstance(stdin, bytes)
    done = subprocess.run(args, input=stdin, capture_output=True, text=text, timeout=timeout, check=False)
    if check and done.returncode:
        raise RuntimeError(f"{' '.join(map(str, args[:4]))} failed: {done.stderr}")
    return done.stdout if done.returncode == 0 else ""


def kubectl(*args, stdin=None, check=True, timeout=180):
    return run("docker", "exec", "-i", K3S, "kubectl", *args, stdin=stdin, check=check, timeout=timeout)


def apply(*items):
    kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(items)}))


def wait_until(condition, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(2)
    raise TimeoutError(f"timed out waiting for {what}")


def remove_k3s():
    run("docker", "rm", "-f", "-v", K3S, check=False)


def start_k3s():
    remove_k3s()
    run("docker", "run", "-d", "--name", K3S, "--privileged", "--tmpfs", "/run", "--tmpfs", "/var/run",
        "-p", "127.0.0.1::6443", "-v", f"{REGISTRIES}:/etc/rancher/k3s/registries.yaml:ro", K3S_IMAGE, "server",
        "--disable", "traefik", "--disable", "servicelb", "--disable", "metrics-server", "--disable", "local-storage")
    wait_until(lambda: " Ready" in kubectl("get", "nodes", check=False), 180, "k3s node")
    architecture = run("docker", "image", "inspect", PY_IMAGE, "--format", "{{.Architecture}}").strip()
    image = subprocess.run(["docker", "save", "--platform", f"linux/{architecture}", PY_IMAGE],
                           capture_output=True, timeout=240, check=True).stdout
    run("docker", "exec", "-i", K3S, "ctr", "-n", "k8s.io", "images", "import", "-", stdin=image, timeout=240)
    run("docker", "exec", K3S, "crictl", "pull", T3_IMAGE, timeout=240)
    wait_until(lambda: "1/1" in kubectl("-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-dns", "--no-headers", check=False),
               240, "cluster DNS")


def write_kubeconfig(folder):
    address = run("docker", "port", K3S, "6443").splitlines()[0].strip()
    config = run("docker", "exec", K3S, "cat", "/etc/rancher/k3s/k3s.yaml").replace("https://127.0.0.1:6443", f"https://{address}")
    path = Path(folder, "kubeconfig")
    path.write_text(config)
    path.chmod(0o600)
    return path


def restricted(**container):
    return {"automountServiceAccountToken": False, "terminationGracePeriodSeconds": 1,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"image": PY_IMAGE, "imagePullPolicy": "Never",
                            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                          "limits": {"cpu": "500m", "memory": "128Mi"}},
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]}},
                            **container}]}


def namespace_object(name, labels):
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels}}


def t3_namespace_labels():
    """T3 렌더 결과에서 Namespace 문서의 라벨을 그대로 읽어 온다."""
    created = kubectl("create", "--dry-run=client", "-o", "json", "-f", "-",
                      stdin=t3_namespace.render(BARE, read_env(PROFILE)).split("\n---\n")[0])
    return json.loads(created)["metadata"]["labels"]


def deploy_fixtures(items):
    """렌더한 target-dev 객체에서 프로세스만 대역으로 바꾼다. 라벨·selector·Service 포트는 그대로다."""
    profile = read_env(PROFILE)
    labels = t3_namespace_labels()
    apply(objects.database("t30rh-node")[0], namespace_object(BARE, labels), namespace_object(NO_LABEL, {}),
          namespace_object(OTHER, {"app.kubernetes.io/part-of": "bravo-target"}),
          namespace_object("bravo-secrets", {"pod-security.kubernetes.io/enforce": "restricted"}))
    kubectl("apply", "--server-side", "--field-manager=rehearsal-runner", "-f", "-", stdin=t3_namespace.render(FULL, profile))

    service = next(i for i in items if i["kind"] == "Service")
    deployment = copy.deepcopy(next(i for i in items if i["kind"] == "Deployment"))
    deployment["spec"]["template"]["spec"] = restricted(
        name="seaweedfs", command=["python3", "-B", "-c", LISTENER], env=[{"name": "PORTS", "value": f"8333,{OTHER_PORT}"}],
        ports=[{"name": "s3", "containerPort": 8333}, {"name": "other", "containerPort": OTHER_PORT}],
        readinessProbe={"tcpSocket": {"port": "s3"}, "periodSeconds": 1})
    openbao_labels = {"app.kubernetes.io/instance": profile["OPENBAO_LABEL_VALUE"], "app.kubernetes.io/name": "openbao"}
    openbao = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "target-openbao", "namespace": "bravo-secrets"},
               "spec": {"replicas": 1, "selector": {"matchLabels": openbao_labels},
                        "template": {"metadata": {"labels": openbao_labels}, "spec": restricted(
                            name="openbao", command=["python3", "-B", "-c", LISTENER],
                            env=[{"name": "PORTS", "value": profile["OPENBAO_PORT"]}],
                            ports=[{"containerPort": int(profile["OPENBAO_PORT"])}],
                            readinessProbe={"tcpSocket": {"port": int(profile["OPENBAO_PORT"])}, "periodSeconds": 1})}}}
    clients = [{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns, "labels": labels},
                "spec": restricted(name="main", command=["python3", "-B", "-c", "import time; time.sleep(10**8)"])}
               for name, (ns, labels) in CLIENTS.items()]
    apply(service, deployment, openbao, *clients)
    for ns in (BARE, FULL, NO_LABEL, OTHER, TARGET, "bravo-secrets"):
        kubectl("-n", ns, "wait", "--for=condition=Ready", "pod", "--all", "--timeout=180s", timeout=200)


def measure(pod_ip):
    def one(cell):
        name, port = cell
        ns = CLIENTS[name][0]
        host = SEAWEED_HOST if port == 8333 else pod_ip  # Service는 8333만 노출한다.
        return cell, kubectl("-n", ns, "exec", name, "--", "python3", "-B", "-c", PROBE, host, str(port),
                             check=False, timeout=30).strip() or "EXEC_FAILED"
    with ThreadPoolExecutor(8) as pool:
        return dict(pool.map(one, [(c, p) for c in CLIENTS for p in (8333, OTHER_PORT)]))


def settle(pod_ip, expected_open, deadline_s=150):
    """집행기 반영을 기다린다. 세 번 연달아 기대와 같아야 인정한다(새 Pod·새 정책의 반영 틈 때문)."""
    started, stable, last = time.monotonic(), 0, {}
    while time.monotonic() - started < deadline_s:
        last = measure(pod_ip)
        unknown = [v for v in last.values() if v != "OPEN" and v not in BLOCKED_SHAPES]
        stable = stable + 1 if not unknown and {c for c, v in last.items() if v == "OPEN"} == expected_open else 0
        if stable == 3:
            break
        time.sleep(2)
    opened = {c for c, v in last.items() if v == "OPEN"}
    return {"as_expected": stable == 3, "seconds": round(time.monotonic() - started, 1),
            "open": sorted(f"{c}:{p}" for c, p in opened),
            "unexpectedly_open": sorted(f"{c}:{p}" for c, p in opened - expected_open),
            "unexpectedly_blocked": sorted(f"{c}:{p}" for c, p in expected_open - opened),
            "responses": dict(collections.Counter(last.values()))}


def t3_gate(kubeconfig, folder):
    """T3 실제 진입점을 그대로 부른다. 세부 문구에 Pod 주소가 있어 ID와 상태만 남긴다."""
    report = Path(folder, f"t3-report-{time.monotonic_ns()}.json")
    done = subprocess.run([str(RUNNER / "bin/create-and-check"), GATE, str(PROFILE), "--report", str(report)],
                          env={**os.environ, "KUBECONFIG": str(kubeconfig)}, capture_output=True, text=True, timeout=420, check=False)
    results = json.loads(report.read_text())["results"] if report.exists() else []
    not_passed = sorted({f"{r['id']}:{r['status'].upper()}" for r in results if r["status"].upper() != "PASS"})
    p4 = [(r["title"].split("(")[0].removeprefix("허용 대상 "), r["status"].upper()) for r in results if r["id"] == "P4"]
    return {"exit_code": done.returncode, "checks": len(results), "not_passed": not_passed, "p4": p4}


def namespace_only(rule):
    for peer in rule["from"]:
        peer.pop("podSelector")


def split_peer_into_or(rule):
    rule["from"] = [{key: peer[key]} for peer in rule["from"] for key in ("namespaceSelector", "podSelector")]


def any_namespace(rule):
    for peer in rule["from"]:
        peer["namespaceSelector"] = {}


def every_port(rule):
    rule.pop("ports")


def only_verifier(rule):
    rule["from"] = rule["from"][:1]


MUTATIONS = (
    (namespace_only, RENDERED | ALL_REHEARSAL_PODS),
    (split_peer_into_or, RENDERED | ALL_REHEARSAL_PODS | on_8333("target-verifier", "target-probe")),
    (any_namespace, RENDERED | on_8333("nolabel-verifier", "nolabel-probe", "other-verifier", "target-verifier", "target-probe")),
    (every_port, RENDERED | {("bare-verifier", OTHER_PORT), ("bare-probe", OTHER_PORT)}),
    (only_verifier, RENDERED - on_8333("bare-probe", "full-probe")),
)
POLICY = "target-object-rehearsal-clients"


def t9_openbao_policy():
    """T9가 렌더한 OpenBao 수신 정책. 주소 인수는 이 측정과 무관한 송신 규칙에만 쓰인다."""
    spec = importlib.util.spec_from_file_location("target_secrets_render", ROOT / "infra/envs/target-secrets/render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.network("10.96.0.1", "192.168.50.10")


def measure_all(folder):
    items = objects.objects("t30rh-node")
    policies = {i["metadata"]["name"]: i for i in items if i["kind"] == "NetworkPolicy"}
    start_k3s()
    kubeconfig = write_kubeconfig(folder)
    deploy_fixtures(items)
    pod_ip = kubectl("-n", TARGET, "get", "pods", "-l", "app.kubernetes.io/name=target-seaweedfs",
                     "-o", "jsonpath={.items[0].status.podIP}").strip()

    states = {"no_policy": settle(pod_ip, LISTENER_CONTROL)}
    apply(*policies.values())
    states["rendered"] = settle(pod_ip, RENDERED)
    gate = {"rendered": [t3_gate(kubeconfig, folder) for _ in range(3)]}

    for mutate, expected in MUTATIONS:
        mutated = copy.deepcopy(policies[POLICY])
        mutate(mutated["spec"]["ingress"][0])
        apply(mutated)
        states[mutate.__name__] = settle(pod_ip, expected)
        if mutate is only_verifier:
            gate["only_verifier"] = t3_gate(kubeconfig, folder)
    kubectl("-n", TARGET, "delete", "networkpolicy", POLICY)
    states["rehearsal_policy_absent"] = settle(pod_ip, RENDERED - on_8333("bare-verifier", "bare-probe", "full-verifier", "full-probe"))
    apply(policies[POLICY])
    states["restored"] = settle(pod_ip, RENDERED)

    # 합격 조건이 아니라 관측이다. T9 소유 정책이 T3 P4의 OpenBao 대조를 받는지 본다.
    apply(t9_openbao_policy())
    time.sleep(5)
    observation = t3_gate(kubeconfig, folder)

    passed = (all(s["as_expected"] for s in states.values())
              and all(g["exit_code"] == 0 for g in gate["rendered"]) and gate["only_verifier"]["exit_code"] == 2
              # 종료 코드 2는 이미지 문제로도 난다. 이 정책 때문에 SeaweedFS P4만 실패했는지까지 본다.
              and gate["only_verifier"]["p4"] == [("seaweedfs", "FAIL"), ("openbao", "PASS")])
    return {"all_as_expected": passed, "states": states, "t3_gate": gate,
            "observation_t3_gate_with_t9_openbao_policy": observation,
            "k3s_version": run("docker", "exec", K3S, "k3s", "--version").splitlines()[0],
            "enforcer": "k3s default: Flannel with the embedded kube-router NetworkPolicy controller",
            "k3s_image": K3S_IMAGE, "standin_image": PY_IMAGE, "t3_image": T3_IMAGE,
            "source_revision": run("git", "-C", str(ROOT), "rev-parse", "HEAD").strip(),
            "worktree_clean": not run("git", "-C", str(ROOT), "status", "--porcelain").strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    # 기본 SIGTERM은 finally를 건너뛰어 privileged 컨테이너를 남긴다. 예외로 바꿔 정리가 돌게 한다.
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    with tempfile.TemporaryDirectory(prefix="t30rh-") as folder:
        try:
            result = measure_all(folder)
        finally:
            remove_k3s()
    (args.out / "results.json").write_text(json.dumps(result, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps({"all_as_expected": result["all_as_expected"]}))
    return 0 if result["all_as_expected"] else 1


if __name__ == "__main__":
    sys.exit(main())
