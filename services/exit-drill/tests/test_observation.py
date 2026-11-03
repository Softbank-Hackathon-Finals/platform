"""kubectl 읽기와 접속 결과를 판정기 입력으로 묶는다. 값을 보기 좋게 고치지 않는다."""

import pytest
from helpers import inventory_doc, snapshot_doc, spec_doc

from bravo_exit_drill.cluster import parse_snapshot
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.observation import (
    ObservationError,
    build_observation,
    normalize_pods,
    probe_request,
)
from bravo_exit_drill.policy import build_plan
from bravo_exit_drill.spec import parse_spec


@pytest.fixture
def plan() -> dict:
    return build_plan(
        parse_spec(spec_doc()),
        parse_inventory(inventory_doc(), candidate="onprem-demo"),
        parse_snapshot(snapshot_doc(), namespace="target-dev"),
    )


def kube_pod(name, labels, *, ready="True", started="2026-11-07T03:00:00Z", restarts=(0,), deleting=False, phase="Running", namespace="target-dev", running_since=("2026-11-07T03:00:01Z",)):
    metadata = {"name": name, "uid": f"uid-{name}", "namespace": namespace, "labels": labels}
    if deleting:
        metadata["deletionTimestamp"] = "2026-11-07T03:05:00Z"
    status = {"phase": phase, "containerStatuses": [
        {"restartCount": r, "state": {"running": {"startedAt": since}} if since else {"waiting": {}}}
        for r, since in zip(restarts, running_since, strict=False)
    ]}
    if started:
        status["startTime"] = started
    if ready is not None:
        status["conditions"] = [{"type": "Ready", "status": ready}]
    return {"kind": "Pod", "metadata": metadata, "status": status}


def pods_doc(*items):
    return {"kind": "List", "items": list(items)}


def test_pods_are_grouped_by_workload(plan):
    got = normalize_pods(pods_doc(
        kube_pod("a1", {"app": "demo-chat"}),
        kube_pod("p1", {"app": "target-pgbouncer"}),
    ), plan)
    assert got["app"] == [{
        "name": "a1", "uid": "uid-a1", "labels": {"app": "demo-chat"}, "ready": True,
        "started_at": "2026-11-07T03:00:00Z", "container_started_at": "2026-11-07T03:00:01Z", "restarts": 0,
        "host_network": False,
    }]
    assert [p["name"] for p in got["pooler"]] == ["p1"]
    assert got["db"] == []


def test_host_network_pod_is_marked(plan):
    pod = kube_pod("a1", {"app": "demo-chat"})
    pod["spec"] = {"hostNetwork": True}
    assert normalize_pods(pods_doc(pod), plan)["app"][0]["host_network"] is True


def test_not_ready_stays_false(plan):
    got = normalize_pods(pods_doc(kube_pod("a1", {"app": "demo-chat"}, ready="False")), plan)
    assert got["app"][0]["ready"] is False


def test_missing_condition_stays_unknown(plan):
    """조건이 없으면 준비 안 됨으로 단정하지 않고 모른다고 둔다."""
    got = normalize_pods(pods_doc(kube_pod("a1", {"app": "demo-chat"}, ready=None)), plan)
    assert got["app"][0]["ready"] is None


def test_restarts_add_up_across_containers(plan):
    got = normalize_pods(pods_doc(kube_pod("a1", {"app": "demo-chat"}, restarts=(1, 2), running_since=("2026-11-07T03:00:01Z", "2026-11-07T03:00:02Z"))), plan)
    assert got["app"][0]["restarts"] == 3


def test_latest_running_container_is_the_last_start(plan):
    got = normalize_pods(pods_doc(kube_pod("a1", {"app": "demo-chat"}, restarts=(0, 1), running_since=("2026-11-07T03:00:05Z", "2026-11-07T03:00:09Z"))), plan)
    assert got["app"][0]["container_started_at"] == "2026-11-07T03:00:09Z"


def test_container_not_running_has_no_start_time(plan):
    got = normalize_pods(pods_doc(kube_pod("a1", {"app": "demo-chat"}, running_since=(None,))), plan)
    assert got["app"][0]["container_started_at"] is None


def test_missing_container_status_stays_unknown(plan):
    pod = kube_pod("a1", {"app": "demo-chat"})
    del pod["status"]["containerStatuses"]
    assert normalize_pods(pods_doc(pod), plan)["app"][0]["restarts"] is None


def test_terminating_and_finished_pods_are_not_counted(plan):
    got = normalize_pods(pods_doc(
        kube_pod("old", {"app": "demo-chat"}, deleting=True),
        kube_pod("job", {"app": "demo-chat"}, phase="Succeeded"),
        kube_pod("new", {"app": "demo-chat"}),
    ), plan)
    assert [p["name"] for p in got["app"]] == ["new"]


def test_pod_outside_every_workload_is_listed(plan):
    observation = build_observation(
        plan, phase="after", pods=pods_doc(
            kube_pod("a1", {"app": "demo-chat"}),
            kube_pod("worker", {"app": "worker"}),
            kube_pod("finished", {"app": "worker"}, phase="Succeeded"),
        ),
        policies_start={"items": []}, policies_end={"items": []},
        probe_runs={}, replication=[], cutover=None, policy_applied_at=None,
    )
    assert observation["unclassified_pods"] == ["worker"]


def test_foreign_namespace_stops(plan):
    with pytest.raises(ObservationError):
        normalize_pods(pods_doc(kube_pod("a1", {"app": "demo-chat"}, namespace="source-dev")), plan)


def test_probe_request_for_owner_lists_only_its_routes(plan):
    request = probe_request(plan, "pooler")
    assert sorted(t["route_id"] for t in request["targets"]) == ["dns", "pooler-to-db", "pooler-to-source"]


def test_probe_request_for_db_lists_tcp_replication_only(plan):
    assert [t["route_id"] for t in probe_request(plan, "db")["targets"]] == ["replication-forward"]


def test_probe_request_for_control_lists_blocks_and_new_connections(plan):
    targets = probe_request(plan, "control")["targets"]
    routes = {t["route_id"] for t in targets if "route_id" in t}
    assert routes == {d["route_id"] for d in plan["decisions"] if d["action"] == "BLOCK"}
    assert {t["check_id"] for t in targets if "check_id" in t} == {c["id"] for c in plan["new_connection_checks"]}


def test_probe_request_for_unknown_vantage_stops(plan):
    with pytest.raises(ObservationError):
        probe_request(plan, "nobody")


def test_build_observation_keeps_inputs_and_binds_plan(plan):
    probes = {"app": {"pod_uid": "uid-a1", "results": [
        {"route_id": "aws-s3", "kind": "tcp", "state": "timeout", "observed_at": "2026-11-07T03:10:00+00:00"},
    ]}}
    control = {"control": {"pod_uid": "uid-c1", "results": [
        {"check_id": "http-ready", "kind": "http", "ok": True, "status": 200, "observed_at": "2026-11-07T03:10:01+00:00"},
    ]}}
    observation = build_observation(
        plan, phase="after", pods=pods_doc(kube_pod("a1", {"app": "demo-chat"})),
        policies_start={"items": []}, policies_end={"items": []},
        probe_runs={**probes, **control},
        replication=[{"route_id": "replication-reverse", "state": "streaming", "lsn": "0/1", "observed_at": "2026-11-07T03:10:00+00:00"}],
        cutover={"writer": "target"}, policy_applied_at="2026-11-07T03:09:00+00:00",
        induced_restarts={"uid-a1": 1},
    )
    assert observation["schema_version"] == "exit-drill-observation/v1"
    assert observation["plan_digest"] == plan["digest"]
    assert observation["policy_applied_at"] == "2026-11-07T03:09:00+00:00"
    assert observation["probes"] == [{
        "route_id": "aws-s3", "vantage": "app", "pod_uid": "uid-a1", "state": "timeout",
        "observed_at": "2026-11-07T03:10:00+00:00",
    }]
    assert observation["app_checks"] == [{"id": "http-ready", "ok": True, "observed_at": "2026-11-07T03:10:01+00:00"}]
    assert observation["cutover"] == {"writer": "target"}
    assert observation["induced_restarts"] == {"uid-a1": 1}


def test_build_observation_for_before_has_no_block_time(plan):
    observation = build_observation(
        plan, phase="before", pods=pods_doc(), policies_start={"items": []}, policies_end={"items": []},
        probe_runs={}, replication=[], cutover=None, policy_applied_at=None,
    )
    assert "policy_applied_at" not in observation
    assert observation["phase"] == "before"


def test_build_observation_rejects_unknown_phase(plan):
    with pytest.raises(ObservationError):
        build_observation(
            plan, phase="during", pods=pods_doc(), policies_start={"items": []}, policies_end={"items": []},
            probe_runs={}, replication=[], cutover=None, policy_applied_at=None,
        )
