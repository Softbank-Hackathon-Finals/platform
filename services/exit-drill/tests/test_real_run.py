"""운영자 도구(scripts/real_run.py). kubectl 대역으로 순서와 기록을 재고, 접속 시험 스크립트는 실제 bash로 잰다."""

import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import inventory_doc, snapshot_doc, spec_doc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import real_run  # noqa: E402

from bravo_exit_drill.cluster import parse_snapshot  # noqa: E402
from bravo_exit_drill.inventory_input import parse_inventory  # noqa: E402
from bravo_exit_drill.policy import build_plan  # noqa: E402
from bravo_exit_drill.spec import parse_spec  # noqa: E402

NOW = "2026-11-07T03:00:00+00:00"
SELECTORS = {
    "app=demo-chat": "demo-chat-1",
    "app=target-pgbouncer": "pgbouncer-1",
    "cnpg.io/cluster=target-db": "target-db-1",
    "bravo-exit-drill-control=true": "control-1",
    "cnpg.io/cluster=target-db,cnpg.io/instanceRole=primary": "target-db-1",
}


@pytest.fixture
def plan() -> dict:
    return build_plan(
        parse_spec(spec_doc()),
        parse_inventory(inventory_doc(), candidate="onprem-demo"),
        parse_snapshot(snapshot_doc(), namespace="target-dev"),
    )


def kube_pod(name: str, *, ready: str = "True", created: str = "2026-11-07T02:00:00Z", deleting: bool = False, statuses=None) -> dict:
    metadata = {"name": name, "uid": f"uid-{name}", "namespace": "target-dev", "creationTimestamp": created, "labels": {}}
    if deleting:
        metadata["deletionTimestamp"] = "2026-11-07T02:30:00Z"
    return {
        "kind": "Pod", "metadata": metadata,
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": ready}], "containerStatuses": statuses or []},
    }


class FakeKube:
    """kubectl 대역. 받은 명령을 순서대로 적고, 미리 정한 값으로 답한다."""

    def __init__(self, plan: dict, *, stream=("SET\nstreaming|0/1000\n", "SET\nstreaming|0/2000\n"), pods=None):
        self.plan = plan
        self.calls: list[tuple] = []
        self.stream = list(stream)
        self.pods = pods or {selector: [kube_pod(name)] for selector, name in SELECTORS.items()}
        self.restarts: dict[tuple[str, str], int] = {}
        self.exec_fails: set[str] = set()

    def __call__(self, *args, stdin=None, check=True):
        self.calls.append(args)
        if args[0] == "get":
            return self._get(args)
        return self._exec(args, stdin)

    def _get(self, args):
        if args[1] == "networkpolicies":
            return json.dumps({"items": []})
        if args[1] == "pod":
            return json.dumps(self._one_pod(args[2]))
        if "-l" in args:
            return json.dumps({"items": self.pods.get(args[args.index("-l") + 1], [])})
        return json.dumps({"kind": "List", "items": []})

    def _one_pod(self, name):
        statuses = []
        for container in ("app", "target-pgbouncer"):
            count = self.restarts.get((name, container), 0)
            statuses.append({"name": container, "restartCount": count, "containerID": f"containerd://{name}-{count}"})
        return kube_pod(name, statuses=statuses)

    def _exec(self, args, stdin):
        pod = args[args.index("-n") + 2]
        if pod in self.exec_fails:
            raise real_run.KubectlError("exec failed")
        command = list(args[args.index("--") + 1:])
        if command[0] == "psql":
            return self.stream.pop(0) if self.stream else ""
        if command[0] == "bash":
            count = (len(command) - 4) // 3
            return "".join(f"connected {NOW}\n" for _ in range(count))
        if command[0] == "sh":
            container = args[args.index("-c") + 1]
            self.restarts[(pod, container)] = self.restarts.get((pod, container), 0) + 1
            return ""
        if command[:2] == ["python", "-c"]:
            return NOW + "\n"
        request = json.loads(command[2])
        assert stdin == real_run.PROBE_SOURCE
        return json.dumps([
            {**{k: t[k] for k in ("route_id", "check_id", "kind") if k in t}, "state": "connected", "ok": True, "observed_at": NOW}
            for t in request["targets"]
        ])

    def execs(self):
        return [c for c in self.calls if c[0] == "exec"]


def pod_of(call: tuple) -> str:
    return call[call.index("-n") + 2]


def collect(fake, plan, **kwargs):
    return real_run.collect(fake, plan, "before", sleep=lambda _: None, **kwargs)


def test_control_measures_around_the_target_pods(plan):
    fake = FakeKube(plan)
    collect(fake, plan)
    pods = [pod_of(c) for c in fake.execs() if "psql" not in c and "-c" not in c[c.index("--") + 1:][:2]]
    assert pods[0] == "control-1" and pods[-1] == "control-1"
    assert {"demo-chat-1", "pgbouncer-1", "target-db-1"} <= {pod_of(c) for c in fake.execs()}


def test_policies_are_read_on_both_sides_of_the_measurement(plan):
    fake = FakeKube(plan)
    collect(fake, plan)
    reads = [i for i, c in enumerate(fake.calls) if c[:2] == ("get", "networkpolicies")]
    execs = [i for i, c in enumerate(fake.calls) if c[0] == "exec"]
    assert reads[0] < execs[0] and reads[-1] > execs[-1]


def test_images_without_python_are_measured_with_bash(plan):
    """풀러 이미지에는 Python이 없다(실제 이미지 확인). 없는 도구를 부르면 그 위치를 재지 못한다."""
    fake = FakeKube(plan)
    collect(fake, plan)
    by_pod = {pod_of(c): c for c in fake.execs() if "psql" not in c}
    assert by_pod["pgbouncer-1"][by_pod["pgbouncer-1"].index("--") + 1] == "bash"
    assert by_pod["pgbouncer-1"][by_pod["pgbouncer-1"].index("-c") + 1] == "target-pgbouncer"
    assert by_pod["target-db-1"][by_pod["target-db-1"].index("-c") + 1] == "postgres"


def test_app_is_measured_in_the_named_container(plan):
    fake = FakeKube(plan)
    collect(fake, plan)
    call = next(c for c in fake.execs() if pod_of(c) == "demo-chat-1")
    assert call[call.index("-c") + 1] == "app"
    assert list(call[call.index("--") + 1:call.index("--") + 3]) == ["python", "-"]


def test_observation_binds_the_plan_and_keeps_inputs(plan):
    fake = FakeKube(plan)
    induced = {"uid-demo-chat-1": {"container": "app", "replaced_container_id": "containerd://x"}}
    observation = real_run.collect(fake, plan, "after", applied_at=NOW, cutover={"writer": "target"}, induced=induced, sleep=lambda _: None)
    assert observation["plan_digest"] == plan["digest"]
    assert observation["phase"] == "after"
    assert observation["policy_applied_at"] == NOW
    assert observation["cutover"] == {"writer": "target"}
    assert observation["induced_restarts"] == induced
    assert {p["vantage"] for p in observation["probes"]} == {"app", "pooler", "db", "control"}
    assert {c["id"] for c in observation["app_checks"]} == {"http-ready", "db-via-pooler", "websocket"}


def test_cutover_is_not_assumed(plan):
    assert collect(FakeKube(plan), plan)["cutover"] is None


def stream_rows(observation):
    return [(r["route_id"], r["state"], r["lsn"]) for r in observation["replication"]]


def test_replication_is_sampled_twice_on_the_primary(plan):
    fake = FakeKube(plan)
    observation = collect(fake, plan)
    assert stream_rows(observation) == [("replication-reverse", "streaming", "0/1000"), ("replication-reverse", "streaming", "0/2000")]
    calls = [c for c in fake.execs() if "psql" in c]
    assert {pod_of(c) for c in calls} == {"target-db-1"}
    assert "default_transaction_read_only=on" in " ".join(calls[0])
    assert "bravo_t8_source_subscriber" in " ".join(calls[0])


def test_no_replication_row_is_recorded_as_absent(plan):
    """구독자가 없다는 것은 읽은 사실이다. 판정이 차단 전 상태와 함께 본다."""
    observation = collect(FakeKube(plan, stream=("SET\n", "SET\n")), plan)
    assert stream_rows(observation)[0] == ("replication-reverse", "absent", None)


def test_unreadable_replication_stays_unknown(plan):
    fake = FakeKube(plan)
    fake.pods["cnpg.io/cluster=target-db,cnpg.io/instanceRole=primary"] = []
    observation = collect(fake, plan)
    assert stream_rows(observation)[0] == ("replication-reverse", None, None)


@pytest.mark.parametrize("name", ["x'; DROP", "a b", ""])
def test_application_name_is_checked_before_it_reaches_sql(plan, name):
    with pytest.raises(ValueError):
        real_run.collect(FakeKube(plan), plan, "before", application_name=name, sleep=lambda _: None)


def test_unready_or_terminating_pod_is_not_measured(plan):
    fake = FakeKube(plan)
    fake.pods["app=demo-chat"] = [kube_pod("gone", deleting=True), kube_pod("down", ready="False")]
    observation = collect(fake, plan)
    assert "app" not in {p["vantage"] for p in observation["probes"]}


def test_newest_ready_pod_is_measured(plan):
    fake = FakeKube(plan)
    fake.pods["app=demo-chat"] = [kube_pod("old"), kube_pod("new", created="2026-11-07T02:40:00Z")]
    collect(fake, plan)
    assert "new" in {pod_of(c) for c in fake.execs()} and "old" not in {pod_of(c) for c in fake.execs()}


def test_failed_measurement_is_left_out_rather_than_invented(plan):
    fake = FakeKube(plan)
    fake.exec_fails.add("pgbouncer-1")
    observation = collect(fake, plan)
    assert "pooler" not in {p["vantage"] for p in observation["probes"]}


def test_probe_garbage_is_left_out(plan):
    assert real_run.parse_probe_output("not json") is None
    assert real_run.parse_probe_output("") is None
    assert real_run.parse_probe_output("{}") is None


@pytest.mark.parametrize("output", ["", f"connected {NOW}\n", f"connected {NOW}\nconnected {NOW}\nconnected {NOW}\n", "connected\nconnected\n"])
def test_shell_output_of_the_wrong_shape_is_left_out(output):
    targets = [{"route_id": "a", "kind": "tcp"}, {"route_id": "b", "kind": "dns"}]
    assert real_run.parse_shell_output(targets, output) is None


def test_shell_output_becomes_the_same_rows_as_the_python_probe():
    targets = [{"route_id": "a", "kind": "tcp"}]
    assert real_run.parse_shell_output(targets, f"refused {NOW}\n") == [{"route_id": "a", "kind": "tcp", "state": "refused", "observed_at": NOW}]


# ---- 재시작 ----


def test_restart_records_the_container_it_replaced(plan):
    fake = FakeKube(plan)
    induced = real_run.restart(fake, plan, ["app"], sleep=lambda _: None)
    assert induced == {"uid-demo-chat-1": {"container": "app", "replaced_container_id": "containerd://demo-chat-1-0"}}


def test_restart_signals_only_the_named_container(plan):
    fake = FakeKube(plan)
    real_run.restart(fake, plan, ["pooler"], sleep=lambda _: None)
    call = next(c for c in fake.execs())
    assert call[call.index("-c") + 1] == "target-pgbouncer"


def test_restart_also_stops_a_child_of_a_shell_pid_one():
    """풀러 이미지의 1번 프로세스는 sh이고 처리기가 없어 TERM을 무시한다. 실제 이미지에서 쟀다."""
    assert real_run.STOP_CONTAINER == "kill -TERM 1; kill -TERM -1"


def test_restart_that_did_not_happen_stops_the_run(plan):
    class Stuck(FakeKube):
        def _exec(self, args, stdin):
            return ""

    with pytest.raises(real_run.RestartNotObserved):
        real_run.restart(Stuck(plan), plan, ["app"], wait_s=0, sleep=lambda _: None)


def test_restart_refuses_a_workload_without_a_named_container(plan):
    with pytest.raises(ValueError):
        real_run.restart(FakeKube(plan), plan, ["db"], sleep=lambda _: None)


def test_restart_of_a_missing_container_stops_clearly(plan):
    plan["workloads"]["app"]["container"] = "other"
    with pytest.raises(real_run.KubectlError):
        real_run.restart(FakeKube(plan), plan, ["app"], sleep=lambda _: None)


def test_tool_has_no_way_to_delete_or_apply():
    """적용 · 되돌리기는 운영자가 안내 문서대로 직접 한다. 이 도구에 넣지 않는다."""
    source = Path(real_run.__file__).read_text(encoding="utf-8")
    for word in ('"delete"', '"apply"', '"patch"', '"scale"', '"rollout"', "DROP", "TRUNCATE", "rm -rf"):
        assert word not in source


def test_clock_reads_the_pod_clock(plan):
    assert real_run.clock(FakeKube(plan), plan) == NOW


# ---- 접속 시험 스크립트. 실제 bash로 실행한다. timeout과 getent만 대역을 쓴다 ----


def shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def tools(tmp_path) -> Path:
    shim(tmp_path, "timeout", 'shift; exec "$@"')
    shim(tmp_path, "getent", '[ "$2" = "known.test" ]')
    return tmp_path


def run_shell_probe(tools: Path, *targets: str) -> list[str]:
    env = {**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}
    done = subprocess.run(["bash", "-c", real_run.SHELL_PROBE, "bash", "1", *targets], capture_output=True, text=True, env=env, timeout=30, check=True)
    return [line.split(" ")[0] for line in done.stdout.splitlines()]


def test_shell_probe_tells_open_from_refused(tools):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        open_port = server.getsockname()[1]
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            closed_port = closed.getsockname()[1]
        states = run_shell_probe(tools, "tcp", "127.0.0.1", str(open_port), "tcp", "127.0.0.1", str(closed_port))
    assert states == ["connected", "refused"]


def test_shell_probe_reports_a_timeout(tools):
    shim(tools, "timeout", "exit 124")
    assert run_shell_probe(tools, "tcp", "127.0.0.1", "1") == ["timeout"]


def test_shell_probe_tells_name_failure_from_denial(tools):
    assert run_shell_probe(tools, "tcp", "no-such-host.invalid", "1") == ["dns_failure"]
    assert run_shell_probe(tools, "dns", "known.test", "0") == ["resolved"]
    assert run_shell_probe(tools, "dns", "other.test", "0") == ["dns_failure"]


def test_shell_probe_sends_nothing(tools):
    """접속만 한다. 데이터를 보내면 DB 쪽 로그에 남는다."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(5)
        run_shell_probe(tools, "tcp", "127.0.0.1", str(server.getsockname()[1]))
        connection, _ = server.accept()
        with connection:
            connection.settimeout(1)
            assert connection.recv(16) == b""


def test_unknown_failure_is_not_counted_as_denied(tools):
    """모르는 실패를 거부로 세면 거짓 차단이 된다. 판정이 UNEXPECTED_STATE로 본다."""
    shim(tools, "timeout", "echo something else >&2; exit 3")
    assert run_shell_probe(tools, "tcp", "127.0.0.1", "1") == ["error"]
