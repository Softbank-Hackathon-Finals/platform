#!/usr/bin/env python3
"""실제 목적지에서 Exit Drill 증거를 모은다. 운영자가 단계마다 직접 실행한다.

계획 · 판정 패키지는 클러스터에 접속하지 않으므로 kubectl을 부르는 일을 여기로 모았다. 읽기와 접속 시험을 하고,
상태를 바꾸는 것은 `restart`의 SIGTERM뿐이다. 정책 적용 · Pod 교체 · 되돌리기는 이 파일에 없다.
순서는 docs/operations/t27-exit-drill-real-run.md를 따른다.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bravo_exit_drill import probe as probe_module
from bravo_exit_drill.observation import build_observation, probe_request

PROBE_SOURCE = Path(probe_module.__file__).read_text(encoding="utf-8")
PROBE_TIMEOUT_S = 3
# 풀러 이미지에는 Python이 없다(이미지에서 확인). CNPG 이미지는 확인하지 못해 같은 길로 잰다.
SHELL_VANTAGES = {"pooler", "db"}
DB_CONTAINER = "postgres"
# 역방향 구독자가 쓰는 이름. infra/envs/target-workloads/replication.py의 external_cluster("source", ...)
REVERSE_APPLICATION_NAME = "bravo_t8_source_subscriber"
SAFE_NAME = re.compile(r"[a-z0-9_]+")
STREAM_SAMPLES = 2
STREAM_INTERVAL_S = 5
RESTART_WAIT_S = 120
# 풀러 이미지의 1번 프로세스는 sh이고 처리기가 없어 TERM을 무시한다. 자식까지 보내야 컨테이너가 끝난다.
STOP_CONTAINER = "kill -TERM 1; kill -TERM -1"
# probe.py와 같은 상태 이름을 쓴다. 판정기가 모르는 실패는 error이고 거부로 세지 않는다.
SHELL_PROBE = r"""
export LC_ALL=C
limit=$1; shift
while [ "$#" -gt 0 ]; do
  kind=$1; host=$2; port=$3; shift 3
  if [ "$kind" = dns ]; then
    if getent hosts "$host" >/dev/null 2>&1; then state=resolved; else state=dns_failure; fi
  else
    message=$(timeout "$limit" bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$host" "$port" 2>&1); code=$?
    case "$code:$message" in
      0:*) state=connected ;;
      124:*) state=timeout ;;
      *"Connection refused"*) state=refused ;;
      *"No route to host"*|*"Network is unreachable"*|*"Permission denied"*|*"Operation not permitted"*) state=unreachable ;;
      *"Name or service not known"*|*"Temporary failure in name resolution"*|*"nodename nor servname"*) state=dns_failure ;;
      *) state=error ;;
    esac
  fi
  printf '%s %s\n' "$state" "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
done
"""
CLOCK = "from datetime import UTC, datetime; print(datetime.now(UTC).isoformat())"

Kube = Callable[..., str]


class KubectlError(RuntimeError):
    """kubectl이 실패했다."""


class RestartNotObserved(RuntimeError):
    """시킨 재시작이 일어나지 않았다. 기록을 남기면 거짓 증거가 된다."""


def kubectl(command: str) -> Kube:
    prefix = shlex.split(command)

    def run(*args: str, stdin: str | None = None) -> str:
        done = subprocess.run([*prefix, *args], input=stdin, capture_output=True, text=True, timeout=300, check=False)
        if done.returncode:
            raise KubectlError(f"{' '.join(args[:3])} failed: {done.stderr.strip()[:300]}")
        return done.stdout

    return run


def _namespace(plan: dict) -> str:
    return plan["namespace"]


def _selector(plan: dict, workload: str, **extra: str) -> str:
    return ",".join(f"{k}={v}" for k, v in {**plan["workloads"][workload]["selector"], **extra}.items())


def _is_ready(pod: dict) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return not pod["metadata"].get("deletionTimestamp") and any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def _ready_pods(kube: Kube, plan: dict, selector: str) -> list[dict]:
    """준비된 Pod를 새것부터. 없으면 그 위치에서는 재지 못하고, 판정이 NOT_OBSERVED로 본다."""
    pods = json.loads(kube("get", "pods", "-n", _namespace(plan), "-l", selector, "-o", "json"))["items"]
    return sorted((p for p in pods if _is_ready(p)), key=lambda p: p["metadata"]["creationTimestamp"], reverse=True)


def parse_probe_output(output: str) -> list[dict] | None:
    try:
        results = json.loads(output)
    except ValueError:
        return None
    return results if isinstance(results, list) else None


def parse_shell_output(targets: list[dict], output: str) -> list[dict] | None:
    rows = [line.split(" ") for line in output.splitlines()]
    if len(rows) != len(targets) or any(len(row) != 2 for row in rows):
        return None
    return [
        {"route_id": target["route_id"], "kind": target["kind"], "state": state, "observed_at": observed_at}
        for target, (state, observed_at) in zip(targets, rows, strict=True)
    ]


def _container(plan: dict, vantage: str) -> str | None:
    return DB_CONTAINER if plan["workloads"][vantage]["role"] == "replication" else plan["workloads"][vantage].get("container")


def measure(kube: Kube, plan: dict, vantage: str) -> dict | None:
    pods = _ready_pods(kube, plan, _selector(plan, vantage))
    if not pods:
        print(f"{vantage}: 준비된 Pod가 없어 재지 못했다", file=sys.stderr)
        return None
    pod = pods[0]["metadata"]
    targets = probe_request(plan, vantage)["targets"]
    container = _container(plan, vantage)
    place = ["exec", "-n", _namespace(plan), pod["name"], *(["-c", container] if container else [])]
    try:
        if vantage in SHELL_VANTAGES:
            address = [v for t in targets for v in (t["kind"], t["host"], str(t.get("port", 0)))]
            results = parse_shell_output(targets, kube(*place, "--", "bash", "-c", SHELL_PROBE, "bash", str(PROBE_TIMEOUT_S), *address))
        else:
            request = json.dumps({"targets": targets, "timeout": PROBE_TIMEOUT_S})
            results = parse_probe_output(kube(*place[:1], "-i", *place[1:], "--", "python", "-", request, stdin=PROBE_SOURCE))
    except KubectlError as exc:
        print(f"{vantage}: {exc}", file=sys.stderr)
        return None
    if results is None:
        print(f"{vantage}: 결과를 읽지 못했다", file=sys.stderr)
        return None
    return {"pod_uid": pod["uid"], "results": results}


def clock(kube: Kube, plan: dict) -> str:
    """대조군 Pod의 시계. 운영자 PC와 어긋나도 Pod 시작 시각과의 전후가 틀어지지 않는다."""
    pods = _ready_pods(kube, plan, _selector(plan, _control(plan)))
    if not pods:
        raise KubectlError("control pod is not ready")
    return kube("exec", "-n", _namespace(plan), pods[0]["metadata"]["name"], "--", "python", "-c", CLOCK).strip()


def _control(plan: dict) -> str:
    return next(name for name, w in plan["workloads"].items() if w["role"] == "control")


def _replication_owner(plan: dict) -> str:
    return next(name for name, w in plan["workloads"].items() if w["role"] == "replication")


def replication_query(application_name: str) -> list[str]:
    """목적지 primary의 walsender를 읽기 전용 세션으로 본다."""
    sql = f"SELECT state, sent_lsn FROM pg_stat_replication WHERE application_name = '{application_name}'"
    return ["psql", "-d", "chat", "-AXt", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", "SET default_transaction_read_only=on", "-c", sql]


def parse_replication(output: str) -> dict:
    """행이 없으면 구독자가 없다는 뜻이다. 차단 전에도 없었으면 판정이 UNKNOWN으로 본다."""
    rows = [line.split("|") for line in output.splitlines() if "|" in line]
    return {"state": rows[0][0], "lsn": rows[0][1]} if rows else {"state": "absent", "lsn": None}


def read_stream(kube: Kube, plan: dict, application_name: str) -> dict:
    """역방향 복제 상태. 못 읽으면 None이고 판정이 UNKNOWN으로 본다."""
    owner = _replication_owner(plan)
    pods = _ready_pods(kube, plan, _selector(plan, owner, **{"cnpg.io/instanceRole": "primary"}))
    if not pods:
        return {"state": None, "lsn": None}
    try:
        return parse_replication(kube("exec", "-n", _namespace(plan), pods[0]["metadata"]["name"], "-c", DB_CONTAINER, "--", *replication_query(application_name)))
    except KubectlError as exc:
        print(f"replication: {exc}", file=sys.stderr)
        return {"state": None, "lsn": None}


def stream_samples(kube: Kube, plan: dict, application_name: str, sleep: Callable[[float], None]) -> list[dict]:
    samples = []
    routes = [d["route_id"] for d in plan["decisions"] if d["action"] == "KEEP" and d["evidence"] == "stream"]
    for index in range(STREAM_SAMPLES):
        if index:
            sleep(STREAM_INTERVAL_S)
        for route_id in routes:
            samples.append({"route_id": route_id, **read_stream(kube, plan, application_name), "observed_at": clock(kube, plan)})
    return samples


def _policies(kube: Kube, plan: dict) -> dict:
    """서버가 덧붙이는 기록만 덜어 낸다. 스펙은 그대로다."""
    doc = json.loads(kube("get", "networkpolicies", "-n", _namespace(plan), "-o", "json"))
    for item in doc.get("items", []):
        item["metadata"].pop("managedFields", None)
        (item["metadata"].get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return doc


def collect(
    kube: Kube,
    plan: dict,
    phase: str,
    *,
    applied_at: str | None = None,
    cutover: dict | None = None,
    induced: dict | None = None,
    application_name: str = REVERSE_APPLICATION_NAME,
    read_replication: Callable[[], list[dict]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """대조군 → 대상 → 대조군 순서로 재서 거부를 차단 탓으로 돌릴 수 있게 한다."""
    if not SAFE_NAME.fullmatch(application_name):
        raise ValueError("application name must be a plain identifier")
    control = _control(plan)
    policies_start = _policies(kube, plan)
    control_first = measure(kube, plan, control)
    owners = {name: measure(kube, plan, name) for name, w in plan["workloads"].items() if w["role"] in {"app", "replication"}}
    control_last = measure(kube, plan, control)
    replication = read_replication() if read_replication else stream_samples(kube, plan, application_name, sleep)
    policies_end = _policies(kube, plan)
    runs: dict[str, Any] = {name: run for name, run in owners.items() if run}
    runs[control] = [run for run in (control_first, control_last) if run]
    pods = json.loads(kube("get", "pods", "-n", _namespace(plan), "-o", "json"))
    return build_observation(
        plan, phase=phase, pods=pods, policies_start=policies_start, policies_end=policies_end,
        probe_runs=runs, replication=replication, cutover=cutover, policy_applied_at=applied_at, induced_restarts=induced,
    )


def _status_of(kube: Kube, plan: dict, pod_name: str, container: str) -> dict:
    pod = json.loads(kube("get", "pod", pod_name, "-n", _namespace(plan), "-o", "json"))
    for status in pod["status"].get("containerStatuses", []):
        if status["name"] == container:
            return status
    raise KubectlError(f"{pod_name} has no container {container}")


def restart(
    kube: Kube, plan: dict, workloads: list[str], *, wait_s: int = RESTART_WAIT_S, sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Pod는 두고 이름 붙은 컨테이너만 다시 띄운다. 이미 정책에 묶인 Pod라 시작 순간부터 차단이 걸려 있다."""
    induced = {}
    for workload in workloads:
        container = plan["workloads"][workload].get("container")
        if not container:
            raise ValueError(f"{workload} has no named container")
        for pod in _ready_pods(kube, plan, _selector(plan, workload)):
            name = pod["metadata"]["name"]
            before = _status_of(kube, plan, name, container)
            try:
                kube("exec", "-n", _namespace(plan), name, "-c", container, "--", "sh", "-c", STOP_CONTAINER)
            except KubectlError:
                pass  # 자기가 든 컨테이너를 끝내므로 exec가 실패로 끝나도 이상이 아니다. 아래에서 결과를 본다.
            deadline = time.monotonic() + wait_s
            while _status_of(kube, plan, name, container).get("restartCount", 0) <= before["restartCount"]:
                if time.monotonic() >= deadline:
                    raise RestartNotObserved(f"{workload}: restart was not observed")
                sleep(2)
            induced[pod["metadata"]["uid"]] = {"container": container, "replaced_container_id": before["containerID"]}
    return induced


def _load(path: str | None) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def _emit(value: Any, path: str | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) if not isinstance(value, str) else value
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", default="kubectl", help="kubectl 명령. 예: 'kubectl --context <확인한 이름>'")
    parser.add_argument("--plan", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("clock", help="대조군 Pod의 현재 시각을 출력한다")
    collecting = commands.add_parser("collect", help="접속 시험과 읽기를 한 번 돌려 관측 파일을 만든다")
    collecting.add_argument("--phase", required=True, choices=["before", "after"])
    collecting.add_argument("--policy-applied-at", help="차단 뒤 관측에 필요. clock으로 잰 값")
    collecting.add_argument("--cutover", help='T12 실행이 끝난 것을 확인했을 때만 {"writer": "target"} 파일을 준다')
    collecting.add_argument("--induced", help="restart가 남긴 기록")
    collecting.add_argument("--out")
    restarting = commands.add_parser("restart", help="앱 · 풀러 컨테이너를 다시 띄우고 기록을 남긴다")
    restarting.add_argument("--workload", action="append", required=True)
    restarting.add_argument("--out")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kube = kubectl(args.kubectl)
    plan = _load(args.plan)
    try:
        if args.command == "clock":
            _emit(clock(kube, plan), None)
        elif args.command == "collect":
            _emit(
                collect(kube, plan, args.phase, applied_at=args.policy_applied_at, cutover=_load(args.cutover), induced=_load(args.induced)),
                args.out,
            )
        else:
            _emit(restart(kube, plan, args.workload), args.out)
    except (KubectlError, RestartNotObserved, ValueError) as exc:
        print(f"real_run: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
