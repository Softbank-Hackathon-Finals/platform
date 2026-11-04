"""차단 뒤 판정. 판정은 코드가 하고, 증거가 빠지거나 불명확하면 통과가 아니라 UNKNOWN이다."""

import copy
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import inventory_doc, network_policy, redigest, snapshot_doc, spec_doc

from bravo_exit_drill import verdict as verdict_module
from bravo_exit_drill.cluster import parse_snapshot
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.observation import normalize_pods
from bravo_exit_drill.policy import build_plan, compute_digest
from bravo_exit_drill.spec import parse_spec
from bravo_exit_drill.verdict import exit_code, judge

BASE = datetime(2026, 11, 7, 3, 0, 0, tzinfo=UTC)
APPLIED = BASE + timedelta(minutes=10)
RESTARTED = APPLIED + timedelta(minutes=1)


def at(seconds: float, base: datetime = RESTARTED) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()


def make_plan(inventory=None) -> dict:
    return build_plan(
        parse_spec(spec_doc()),
        parse_inventory(inventory_doc() if inventory is None else inventory, candidate="onprem-demo"),
        parse_snapshot(snapshot_doc(), namespace="target-dev"),
    )


def _pods(plan: dict, suffix: str, started: datetime) -> dict:
    return {
        name: [{
            "name": f"{name}-{suffix}",
            "uid": f"{name}-{suffix}",
            "labels": dict(item["selector"]),
            "ready": True,
            "started_at": started.isoformat(),
            "host_network": False,
            "container_name": item.get("container"),
            "container_id": f"containerd://{name}-{suffix}-1",
            "previous_container_id": None,
            "container_started_at": started.isoformat(),
            "restarts": 0,
            "other_restarts": 0,
        }]
        for name, item in plan["workloads"].items()
    }


def _probes(plan: dict, pods: dict, *, blocked: bool, start: datetime) -> list[dict]:
    """대조군 → 대상 → 대조군 순서로 잰 것처럼 만든다."""
    rows = []
    clock = [start]

    def add(route_id, vantage, state):
        clock[0] += timedelta(seconds=1)
        rows.append({
            "route_id": route_id,
            "vantage": vantage,
            "pod_uid": pods[vantage][0]["uid"],
            "state": state,
            "observed_at": clock[0].isoformat(),
        })

    for decision in plan["decisions"]:
        if decision["probe"] is None:
            continue
        open_state = "resolved" if decision["probe"]["kind"] == "dns" else "connected"
        for owner in decision["owners"]:
            if decision["action"] == "BLOCK":
                add(decision["route_id"], "control", "connected")
                add(decision["route_id"], owner, "timeout" if blocked else "connected")
                add(decision["route_id"], "control", "connected")
            else:
                add(decision["route_id"], owner, open_state)
    return rows


def good_evidence(plan: dict) -> tuple[dict, dict]:
    before_pods = _pods(plan, "old", BASE)
    after_pods = _pods(plan, "old", BASE)
    induced = {}
    for name in ("app", "pooler"):
        after_pods[name] = _pods(plan, "new", RESTARTED)[name]
        replaced = after_pods[name][0]["container_id"]
        after_pods[name][0].update(
            restarts=1, container_started_at=at(10),
            container_id=f"containerd://{name}-new-2", previous_container_id=replaced,
        )
        induced[after_pods[name][0]["uid"]] = {"container": plan["workloads"][name]["container"], "replaced_container_id": replaced}
    applied_manifests = copy.deepcopy(plan["manifests"])
    for manifest in applied_manifests:
        manifest["metadata"]["creationTimestamp"] = at(1, APPLIED)
    before = {
        "schema_version": "exit-drill-observation/v1",
        "plan_digest": plan["digest"],
        "phase": "before",
        "pods": before_pods,
        "policy_readback": {"start": [], "end": []},
        "probes": _probes(plan, before_pods, blocked=False, start=BASE),
        "replication": [
            {"route_id": "replication-reverse", "state": "streaming", "lsn": "0/1000", "observed_at": at(1, BASE)},
        ],
        "app_checks": [],
    }
    after = {
        "schema_version": "exit-drill-observation/v1",
        "plan_digest": plan["digest"],
        "phase": "after",
        "policy_applied_at": APPLIED.isoformat(),
        "cutover": {"writer": "target"},
        "pods": after_pods,
        "induced_restarts": induced,
        "unclassified_pods": [],
        "policy_readback": {
            "start": copy.deepcopy(applied_manifests),
            "end": copy.deepcopy(applied_manifests),
        },
        "probes": _probes(plan, after_pods, blocked=True, start=RESTARTED + timedelta(seconds=30)),
        "replication": [
            {"route_id": "replication-reverse", "state": "streaming", "lsn": "0/2000", "observed_at": at(40)},
            {"route_id": "replication-reverse", "state": "streaming", "lsn": "0/2800", "observed_at": at(50)},
        ],
        "app_checks": [{"id": c["id"], "ok": True, "observed_at": at(60)} for c in plan["new_connection_checks"]],
    }
    return before, after


@pytest.fixture
def plan() -> dict:
    return make_plan()


def result(plan: dict, before: dict, after: dict) -> tuple[str, dict]:
    report = judge(plan, before, after)
    return report["verdict"], {c["id"]: c for c in report["checks"]}


def codes(checks: dict) -> set[str]:
    return {c["code"] for c in checks.values()}


def rows(observation: dict, route_id: str, vantage: str) -> list[dict]:
    return [p for p in observation["probes"] if p["route_id"] == route_id and p["vantage"] == vantage]


def test_complete_good_evidence_passes(plan):
    before, after = good_evidence(plan)
    verdict, checks = result(plan, before, after)
    assert verdict == "PASS", [c for c in checks.values() if c["verdict"] != "PASS"]
    assert {"block:aws-s3:app", "block:pooler-to-source:pooler", "local:dns:app", "keep:replication-forward:db",
            "keep:replication-reverse:stream", "restart:app", "restart:pooler", "new-connection:websocket"} <= set(checks)


def test_report_binds_plan_and_counts(plan):
    before, after = good_evidence(plan)
    report = judge(plan, before, after)
    assert report["plan_digest"] == plan["digest"]
    assert report["summary"]["FAIL"] == 0
    assert report["summary"]["PASS"] == len(report["checks"])


def test_judging_is_deterministic(plan):
    before, after = good_evidence(plan)
    assert judge(plan, before, after) == judge(plan, copy.deepcopy(before), copy.deepcopy(after))


@pytest.mark.parametrize(("verdict", "code"), [("PASS", 0), ("FAIL", 1), ("UNKNOWN", 3)])
def test_exit_codes_tell_verdicts_apart(verdict, code):
    assert exit_code({"verdict": verdict}) == code


# ---- AWS 경로 ----


def test_open_aws_path_fails(plan):
    before, after = good_evidence(plan)
    rows(after, "aws-s3", "app")[0]["state"] = "connected"
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["block:aws-s3:app"]["code"] == "SOURCE_PATH_OPEN"


def test_fail_beats_unknown(plan):
    before, after = good_evidence(plan)
    rows(after, "aws-s3", "app")[0]["state"] = "connected"
    after["app_checks"] = []
    assert result(plan, before, after)[0] == "FAIL"


def test_one_connected_among_denied_samples_still_fails(plan):
    before, after = good_evidence(plan)
    sample = dict(rows(after, "aws-s3", "app")[0], state="connected")
    after["probes"].append(sample)
    assert result(plan, before, after)[0] == "FAIL"


def test_open_source_pooler_path_fails(plan):
    before, after = good_evidence(plan)
    rows(after, "pooler-to-source", "pooler")[0]["state"] = "connected"
    assert result(plan, before, after)[0] == "FAIL"


@pytest.mark.parametrize("state", ["refused", "unreachable", "timeout"])
def test_denial_states_with_controls_pass(plan, state):
    before, after = good_evidence(plan)
    rows(after, "aws-s3", "app")[0]["state"] = state
    assert result(plan, before, after)[0] == "PASS"


@pytest.mark.parametrize("state", ["dns_failure", "error", "banana"])
def test_ambiguous_failure_is_not_denial(plan, state):
    """이름 풀이 실패는 DNS가 고장 나도 똑같이 보인다. 차단의 증거가 아니다."""
    before, after = good_evidence(plan)
    rows(after, "aws-s3", "app")[0]["state"] = state
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["block:aws-s3:app"]["code"] == "UNEXPECTED_STATE"


def test_missing_block_observation_is_unknown(plan):
    before, after = good_evidence(plan)
    after["probes"] = [p for p in after["probes"] if not (p["route_id"] == "aws-kms" and p["vantage"] == "app")]
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["block:aws-kms:app"]["code"] == "NOT_OBSERVED"


def test_path_never_open_before_block_is_unknown(plan):
    """차단 전에도 닫혀 있었다면 지금 닫혀 있는 것이 정책 덕분인지 알 수 없다."""
    before, after = good_evidence(plan)
    rows(before, "aws-s3", "app")[0]["state"] = "timeout"
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["block:aws-s3:app"]["code"] == "BASELINE_NOT_OPEN"


def test_failed_positive_control_is_unknown(plan):
    """대상이 내려가 있으면 거부처럼 보인다. 대조군이 닿지 않으면 판정하지 않는다."""
    before, after = good_evidence(plan)
    rows(after, "aws-s3", "control")[0]["state"] = "timeout"
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["block:aws-s3:app"]["code"] == "POSITIVE_CONTROL_FAILED"


def test_control_after_target_failing_is_unknown(plan):
    before, after = good_evidence(plan)
    rows(after, "aws-s3", "control")[1]["state"] = "refused"
    assert result(plan, before, after)[1]["block:aws-s3:app"]["code"] == "POSITIVE_CONTROL_FAILED"


def test_control_must_surround_the_target_in_time(plan):
    before, after = good_evidence(plan)
    target = rows(after, "aws-s3", "app")[0]
    controls = rows(after, "aws-s3", "control")
    controls[1]["observed_at"] = controls[0]["observed_at"]
    assert target["observed_at"] > controls[1]["observed_at"]
    assert result(plan, before, after)[1]["block:aws-s3:app"]["code"] == "POSITIVE_CONTROL_FAILED"


def test_missing_control_is_unknown(plan):
    before, after = good_evidence(plan)
    after["probes"] = [p for p in after["probes"] if not (p["route_id"] == "aws-s3" and p["vantage"] == "control")]
    assert result(plan, before, after)[1]["block:aws-s3:app"]["code"] == "POSITIVE_CONTROL_FAILED"


def test_control_from_an_unknown_pod_is_unknown(plan):
    before, after = good_evidence(plan)
    for row in rows(after, "aws-s3", "control"):
        row["pod_uid"] = "someone-else"
    assert result(plan, before, after)[1]["block:aws-s3:app"]["code"] == "POSITIVE_CONTROL_FAILED"


def test_probe_from_a_pod_outside_the_workload_is_unknown(plan):
    before, after = good_evidence(plan)
    rows(after, "aws-s3", "app")[0]["pod_uid"] = "someone-else"
    assert result(plan, before, after)[1]["block:aws-s3:app"]["code"] == "VANTAGE_NOT_BOUND"


# ---- 목적지 안 경로 ----


def test_broken_local_path_fails(plan):
    before, after = good_evidence(plan)
    rows(after, "app-to-pooler", "app")[0]["state"] = "timeout"
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["local:app-to-pooler:app"]["code"] == "LOCAL_PATH_BROKEN"


def test_broken_dns_fails(plan):
    before, after = good_evidence(plan)
    rows(after, "dns", "app")[0]["state"] = "dns_failure"
    assert result(plan, before, after)[1]["local:dns:app"]["code"] == "LOCAL_PATH_BROKEN"


def test_local_path_broken_before_block_too_is_unknown(plan):
    before, after = good_evidence(plan)
    rows(before, "app-to-pooler", "app")[0]["state"] = "timeout"
    rows(after, "app-to-pooler", "app")[0]["state"] = "timeout"
    assert result(plan, before, after)[1]["local:app-to-pooler:app"]["code"] == "BASELINE_NOT_OPEN"


def test_missing_local_observation_is_unknown(plan):
    before, after = good_evidence(plan)
    after["probes"] = [p for p in after["probes"] if p["route_id"] != "app-to-objects"]
    assert result(plan, before, after)[1]["local:app-to-objects:app"]["code"] == "NOT_OBSERVED"


# ---- 복제 채널 ----


def test_broken_replication_path_fails(plan):
    before, after = good_evidence(plan)
    rows(after, "replication-forward", "db")[0]["state"] = "timeout"
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["keep:replication-forward:db"]["code"] == "REPLICATION_PATH_BROKEN"


def test_replication_path_closed_before_block_too_is_unknown(plan):
    before, after = good_evidence(plan)
    rows(before, "replication-forward", "db")[0]["state"] = "timeout"
    rows(after, "replication-forward", "db")[0]["state"] = "timeout"
    assert result(plan, before, after)[1]["keep:replication-forward:db"]["code"] == "BASELINE_NOT_OPEN"


def test_stopped_stream_fails(plan):
    before, after = good_evidence(plan)
    after["replication"][1]["state"] = "catchup"
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["keep:replication-reverse:stream"]["code"] == "REPLICATION_NOT_STREAMING"


def test_idle_stream_is_unknown_not_pass(plan):
    """쓰기가 없어 LSN이 안 움직이면 채널이 살아 있는지 모른다."""
    before, after = good_evidence(plan)
    after["replication"][1]["lsn"] = after["replication"][0]["lsn"]
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["keep:replication-reverse:stream"]["code"] == "REPLICATION_IDLE"


def test_lsn_compares_as_number_not_text(plan):
    before, after = good_evidence(plan)
    after["replication"][0]["lsn"] = "0/9000"
    after["replication"][1]["lsn"] = "0/10000"
    assert result(plan, before, after)[0] == "PASS"


def test_lsn_high_part_counts(plan):
    before, after = good_evidence(plan)
    after["replication"][0]["lsn"] = "0/FFFFFFFF"
    after["replication"][1]["lsn"] = "1/0"
    assert result(plan, before, after)[0] == "PASS"


def test_single_sample_is_unknown(plan):
    before, after = good_evidence(plan)
    after["replication"].pop()
    assert result(plan, before, after)[1]["keep:replication-reverse:stream"]["code"] == "NOT_OBSERVED"


def test_unreadable_lsn_is_unknown(plan):
    before, after = good_evidence(plan)
    after["replication"][1]["lsn"] = "soon"
    assert result(plan, before, after)[1]["keep:replication-reverse:stream"]["code"] == "LSN_UNREADABLE"


def test_stream_not_running_before_block_is_unknown(plan):
    before, after = good_evidence(plan)
    before["replication"] = []
    assert result(plan, before, after)[1]["keep:replication-reverse:stream"]["code"] == "BASELINE_NOT_STREAMING"


# ---- 재시작 ----


def test_pods_that_did_not_restart_are_unknown(plan):
    """정책만 넣고 재시작을 안 하면 재시작 시험을 통과한 것이 아니다."""
    before, after = good_evidence(plan)
    after["pods"]["app"] = copy.deepcopy(before["pods"]["app"])
    for row in after["probes"]:
        if row["vantage"] == "app":
            row["pod_uid"] = after["pods"]["app"][0]["uid"]
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "RESTART_NOT_OBSERVED"


def test_pod_not_ready_after_restart_fails(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["ready"] = False
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["restart:app"]["code"] == "NOT_READY"


def test_unknown_readiness_is_unknown(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["ready"] = None
    assert result(plan, before, after)[1]["restart:app"]["code"] == "READINESS_UNKNOWN"


def test_no_pods_after_restart_fails(plan):
    before, after = good_evidence(plan)
    after["pods"]["pooler"] = []
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["restart:pooler"]["code"] == "NO_PODS"


def test_pod_list_not_read_is_unknown(plan):
    before, after = good_evidence(plan)
    del after["pods"]["pooler"]
    assert result(plan, before, after)[1]["restart:pooler"]["code"] == "NOT_OBSERVED"


def test_baseline_pods_not_read_is_unknown(plan):
    before, after = good_evidence(plan)
    del before["pods"]["app"]
    assert result(plan, before, after)[1]["restart:app"]["code"] == "NOT_OBSERVED"


def test_started_before_block_is_unknown(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["started_at"] = at(-1, APPLIED)
    assert result(plan, before, after)[1]["restart:app"]["code"] == "STARTED_BEFORE_BLOCK"


def test_unreadable_start_time_is_unknown(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["started_at"] = "2026-11-07 03:00:00"
    assert result(plan, before, after)[1]["restart:app"]["code"] == "STARTED_BEFORE_BLOCK"


def test_pod_replacement_alone_does_not_prove_startup(plan):
    """정책이 있어도 새 Pod는 처음 잠깐 AWS에 닿는다(k3s에서 실측). 그 사이에 뜬 앱은 증거가 아니다."""
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["restarts"] = 0
    del after["induced_restarts"][after["pods"]["app"][0]["uid"]]
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "STARTUP_NOT_ISOLATED"


def test_claimed_in_place_restart_that_did_not_happen_is_unknown(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["restarts"] = 0
    assert result(plan, before, after)[1]["restart:app"]["code"] == "STARTUP_NOT_ISOLATED"


def test_missing_induced_record_is_unknown(plan):
    before, after = good_evidence(plan)
    del after["induced_restarts"]
    assert result(plan, before, after)[1]["restart:app"]["code"] == "UNSTABLE_RESTARTS"


def test_in_place_restart_too_soon_after_pod_start_is_unknown(plan):
    """새 Pod가 정책에 묶이기 전에 컨테이너를 다시 띄우면 틈이 그대로다."""
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["container_started_at"] = at(1)
    assert result(plan, before, after)[1]["restart:app"]["code"] == "STARTUP_NOT_ISOLATED"


def test_policy_created_after_the_restart_is_unknown(plan):
    """적용 시각은 사람이 적는 값이다. 클러스터가 기록한 생성 시각이 재시작보다 늦으면 믿지 않는다."""
    before, after = good_evidence(plan)
    for side in ("start", "end"):
        for manifest in after["policy_readback"][side]:
            manifest["metadata"]["creationTimestamp"] = at(30)
    assert result(plan, before, after)[1]["restart:app"]["code"] == "STARTED_BEFORE_BLOCK"


def test_policy_created_right_before_the_restart_is_unknown(plan):
    before, after = good_evidence(plan)
    for side in ("start", "end"):
        for manifest in after["policy_readback"][side]:
            manifest["metadata"]["creationTimestamp"] = at(9)
    assert result(plan, before, after)[1]["restart:app"]["code"] == "STARTED_BEFORE_BLOCK"


def test_unknown_policy_creation_time_is_unknown(plan):
    before, after = good_evidence(plan)
    for side in ("start", "end"):
        for manifest in after["policy_readback"][side]:
            del manifest["metadata"]["creationTimestamp"]
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "POLICY_TIME_UNKNOWN"


def test_crash_beyond_the_induced_restart_is_unknown(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["restarts"] = 2
    assert result(plan, before, after)[1]["restart:app"]["code"] == "UNSTABLE_RESTARTS"


def test_crash_nobody_caused_is_unknown(plan):
    before, after = good_evidence(plan)
    after["induced_restarts"] = {}
    assert result(plan, before, after)[1]["restart:app"]["code"] == "UNSTABLE_RESTARTS"


@pytest.mark.parametrize("field", ["container_name", "container_id", "restarts", "other_restarts"])
@pytest.mark.parametrize("change", [lambda p, k: p.pop(k), lambda p, k: p.update({k: None}), lambda p, k: p.update({k: "x" if k.endswith("restarts") else ""})])
def test_missing_or_malformed_container_state_is_unknown(plan, field, change):
    before, after = good_evidence(plan)
    change(after["pods"]["app"][0], field)
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert (checks["restart:app"]["verdict"], checks["restart:app"]["code"]) == ("UNKNOWN", "CONTAINER_UNKNOWN")


def test_state_of_another_container_is_unknown(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["container_name"] = "collector"
    assert result(plan, before, after)[1]["restart:app"]["code"] == "CONTAINER_UNKNOWN"


def test_plan_without_container_name_cannot_pass(plan):
    """이름 없는 대상과 이름 없는 관측이 같다고 통과하면 안 된다."""
    before, after = good_evidence(plan)
    del plan["workloads"]["app"]["container"]
    plan["digest"] = before["plan_digest"] = after["plan_digest"] = compute_digest(plan)
    after["pods"]["app"][0]["container_name"] = None
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "CONTAINER_UNKNOWN"


def test_not_ready_still_fails_when_the_container_is_not_running(plan):
    """시작하지 못한 컨테이너는 상태가 비어 있다. 모른다가 아니라 앱이 안 뜬 것이다."""
    before, after = good_evidence(plan)
    after["pods"]["app"][0].update(ready=False, container_started_at=None, container_id=None)
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["restart:app"]["code"] == "NOT_READY"


@pytest.mark.parametrize("record", [
    1,
    "yes",
    {},
    {"container": "app"},
    {"replaced_container_id": "containerd://app-new-1"},
    {"container": "collector", "replaced_container_id": "containerd://app-new-1"},
    {"container": "app", "replaced_container_id": "containerd://someone-else"},
    {"container": "app", "replaced_container_id": None},
])
def test_record_that_does_not_match_the_observed_restart_is_unknown(plan, record):
    before, after = good_evidence(plan)
    pod = after["pods"]["app"][0]
    after["induced_restarts"][pod["uid"]] = record
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "RESTART_RECORD_MISMATCH"


def test_container_that_kept_its_id_did_not_restart(plan):
    """횟수가 같아도 지금 도는 컨테이너가 기록의 그 컨테이너면 다시 뜬 것이 아니다."""
    before, after = good_evidence(plan)
    pod = after["pods"]["app"][0]
    pod["container_id"] = pod["previous_container_id"]
    assert result(plan, before, after)[1]["restart:app"]["code"] == "RESTART_RECORD_MISMATCH"


BLANK_IDS = ["", " ", "\t\n"]


@pytest.mark.parametrize("blank", BLANK_IDS)
def test_blank_ids_in_record_and_last_state_prove_nothing(plan, blank):
    """빈 값끼리 같아도 어떤 컨테이너의 재시작을 승인했는지 알 수 없다(PR #165 리뷰)."""
    before, after = good_evidence(plan)
    pod = after["pods"]["app"][0]
    pod["previous_container_id"] = blank
    after["induced_restarts"][pod["uid"]]["replaced_container_id"] = blank
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert (checks["restart:app"]["verdict"], checks["restart:app"]["code"]) == ("UNKNOWN", "RESTART_RECORD_MISMATCH")


@pytest.mark.parametrize("blank", BLANK_IDS)
def test_blank_running_container_id_is_unknown(plan, blank):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["container_id"] = blank
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert (checks["restart:app"]["verdict"], checks["restart:app"]["code"]) == ("UNKNOWN", "CONTAINER_UNKNOWN")


# ---- 여러 컨테이너. kubectl이 주는 모양을 normalize_pods로 읽어 판정까지 잇는다 ----


def kube_pod(row: dict, plan: dict, statuses: list[dict], init_statuses=()) -> dict:
    status = {
        "phase": "Running",
        "startTime": row["started_at"],
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": statuses,
    }
    if init_statuses:
        status["initContainerStatuses"] = list(init_statuses)
    return {
        "kind": "Pod",
        "metadata": {"name": row["name"], "uid": row["uid"], "namespace": plan["namespace"], "labels": row["labels"]},
        "status": status,
    }


def container(name: str, restarts: int, *, since: str, current: str = "2", previous: str | None = "1") -> dict:
    status: dict = {
        "name": name,
        "restartCount": restarts,
        "containerID": f"containerd://{name}-{current}",
        "state": {"running": {"startedAt": since}},
    }
    if previous:
        status["lastState"] = {"terminated": {"containerID": f"containerd://{name}-{previous}"}}
    return status


def evidence_with_app_containers(plan: dict, statuses: list[dict], init_statuses=()) -> tuple[dict, dict]:
    before, after = good_evidence(plan)
    row = after["pods"]["app"][0]
    after["pods"]["app"] = normalize_pods({"items": [kube_pod(row, plan, statuses, init_statuses)]}, plan)["app"]
    after["induced_restarts"][row["uid"]] = {"container": "app", "replaced_container_id": "containerd://app-1"}
    return before, after


def test_sidecar_restart_does_not_prove_the_app_restarted(plan):
    """앱 0회 · 사이드카 1회. 합산하면 시킨 재시작이 일어난 것처럼 읽혀 전체 PASS가 됐다(PR #165 리뷰)."""
    before, after = evidence_with_app_containers(plan, [
        container("app", 0, since=at(1), current="1", previous=None),
        container("collector", 1, since=at(10)),
    ])
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert (checks["restart:app"]["verdict"], checks["restart:app"]["code"]) == ("UNKNOWN", "STARTUP_NOT_ISOLATED")


def test_approved_app_container_restart_passes_beside_a_quiet_sidecar(plan):
    before, after = evidence_with_app_containers(plan, [
        container("app", 1, since=at(10)),
        container("collector", 0, since=at(1), current="1", previous=None),
    ])
    verdict, checks = result(plan, before, after)
    assert verdict == "PASS", [c for c in checks.values() if c["verdict"] != "PASS"]
    assert checks["restart:app"]["code"] == "RESTARTED_READY"


@pytest.mark.parametrize("blank", BLANK_IDS)
def test_blank_last_state_id_from_kubectl_is_unknown(plan, blank):
    app = container("app", 1, since=at(10))
    app["lastState"]["terminated"]["containerID"] = blank
    before, after = evidence_with_app_containers(plan, [app])
    after["induced_restarts"][after["pods"]["app"][0]["uid"]]["replaced_container_id"] = blank
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "RESTART_RECORD_MISMATCH"


@pytest.mark.parametrize("blank", BLANK_IDS)
def test_blank_running_id_from_kubectl_is_unknown(plan, blank):
    app = container("app", 1, since=at(10))
    app["containerID"] = blank
    before, after = evidence_with_app_containers(plan, [app])
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "CONTAINER_UNKNOWN"


def test_sidecar_listed_first_is_not_mistaken_for_the_app(plan):
    before, after = evidence_with_app_containers(plan, [
        container("collector", 0, since=at(1), current="1", previous=None),
        container("app", 1, since=at(10)),
    ])
    assert result(plan, before, after)[0] == "PASS"


def test_sidecar_crash_beside_the_approved_restart_is_unknown(plan):
    """차단 아래에서 사이드카가 죽었다. 근거로 세지 않지만 없던 일로 넘기지도 않는다."""
    before, after = evidence_with_app_containers(plan, [
        container("app", 1, since=at(10)),
        container("collector", 1, since=at(5)),
    ])
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "OTHER_CONTAINER_RESTARTED"


def test_restartable_init_sidecar_crash_is_unknown(plan):
    before, after = evidence_with_app_containers(
        plan, [container("app", 1, since=at(10))], init_statuses=[container("tailscale", 2, since=at(5))],
    )
    assert result(plan, before, after)[1]["restart:app"]["code"] == "OTHER_CONTAINER_RESTARTED"


@pytest.mark.parametrize("statuses", [
    [],
    [{"restartCount": 1, "containerID": "containerd://x", "state": {"running": {"startedAt": at(10)}}}],
    [container("collector", 1, since=at(10))],
    [container("app", 1, since=at(10)), container("app", 1, since=at(10))],
])
def test_missing_or_ambiguous_container_status_is_unknown(plan, statuses):
    before, after = evidence_with_app_containers(plan, statuses)
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["restart:app"]["code"] == "CONTAINER_UNKNOWN"


def test_planned_name_found_only_among_init_containers_is_unknown(plan):
    before, after = evidence_with_app_containers(plan, [container("collector", 0, since=at(1))], init_statuses=[container("app", 1, since=at(10))])
    assert result(plan, before, after)[1]["restart:app"]["code"] == "CONTAINER_UNKNOWN"


def test_container_not_running_has_no_start_to_trust(plan):
    waiting = container("app", 1, since=at(10))
    waiting["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
    before, after = evidence_with_app_containers(plan, [waiting])
    assert result(plan, before, after)[1]["restart:app"]["code"] == "STARTED_BEFORE_BLOCK"


def test_slow_sidecar_start_does_not_move_the_new_connection_bar(plan):
    """새 연결은 앱 컨테이너가 다시 뜬 뒤면 된다. 재시작 없이 늦게 뜬 사이드카는 기준이 아니다."""
    before, after = evidence_with_app_containers(plan, [
        container("app", 1, since=at(10)),
        container("collector", 0, since=at(100), current="1", previous=None),
    ])
    assert result(plan, before, after)[0] == "PASS"


# ---- 새 연결 ----


def test_failed_new_connection_fails(plan):
    before, after = good_evidence(plan)
    after["app_checks"][0]["ok"] = False
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks[f"new-connection:{after['app_checks'][0]['id']}"]["code"] == "NEW_CONNECTION_FAILED"


def test_missing_new_connection_is_unknown(plan):
    before, after = good_evidence(plan)
    after["app_checks"] = after["app_checks"][1:]
    assert result(plan, before, after)[0] == "UNKNOWN"


def test_connection_from_before_restart_is_not_new(plan):
    before, after = good_evidence(plan)
    after["app_checks"][0]["observed_at"] = at(-1)
    check = result(plan, before, after)[1][f"new-connection:{after['app_checks'][0]['id']}"]
    assert check["code"] == "CHECK_BEFORE_RESTART"


def test_connection_before_the_in_place_restart_is_not_new(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["container_started_at"] = at(61)
    check = result(plan, before, after)[1][f"new-connection:{after['app_checks'][0]['id']}"]
    assert check["code"] == "CHECK_BEFORE_RESTART"


def test_unreadable_container_start_means_connection_is_not_proven_new(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["container_started_at"] = "soon"
    check = result(plan, before, after)[1][f"new-connection:{after['app_checks'][0]['id']}"]
    assert check["code"] == "CHECK_BEFORE_RESTART"


def test_non_boolean_ok_is_unknown(plan):
    before, after = good_evidence(plan)
    after["app_checks"][0]["ok"] = "yes"
    check = result(plan, before, after)[1][f"new-connection:{after['app_checks'][0]['id']}"]
    assert (check["verdict"], check["code"]) == ("UNKNOWN", "CHECK_UNREADABLE")


# ---- 증거 결속 ----


def test_evidence_for_another_plan_is_unknown_and_stops(plan):
    before, after = good_evidence(plan)
    after["plan_digest"] = "sha256:other"
    rows(after, "aws-s3", "app")[0]["state"] = "connected"
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert "PLAN_NOT_BOUND" in codes(checks)
    assert "block:aws-s3:app" not in checks


@pytest.mark.parametrize("side", ["before", "after"])
def test_unknown_observation_version_is_unknown(plan, side):
    before, after = good_evidence(plan)
    {"before": before, "after": after}[side]["schema_version"] = "v9"
    assert result(plan, before, after)[0] == "UNKNOWN"


def test_swapped_phases_are_unknown(plan):
    before, after = good_evidence(plan)
    verdict, checks = result(plan, after, before)
    assert verdict == "UNKNOWN"
    assert "PHASE_MISMATCH" in codes(checks)


def test_before_measured_after_block_is_unknown(plan):
    before, after = good_evidence(plan)
    before["probes"][0]["observed_at"] = at(0, APPLIED)
    assert "OBSERVED_OUTSIDE_PHASE" in codes(result(plan, before, after)[1])


def test_after_measured_before_block_is_unknown(plan):
    before, after = good_evidence(plan)
    after["probes"][0]["observed_at"] = at(-1, APPLIED)
    assert "OBSERVED_OUTSIDE_PHASE" in codes(result(plan, before, after)[1])


def test_after_sample_with_unreadable_time_is_unknown(plan):
    before, after = good_evidence(plan)
    after["replication"][0]["observed_at"] = "yesterday"
    assert "OBSERVED_OUTSIDE_PHASE" in codes(result(plan, before, after)[1])


def test_unreadable_block_time_is_unknown(plan):
    before, after = good_evidence(plan)
    after["policy_applied_at"] = "2026-11-07T03:00:00"  # 시간대 없음
    assert "OBSERVED_OUTSIDE_PHASE" in codes(result(plan, before, after)[1])


def test_stretched_observation_window_is_unknown(plan):
    before, after = good_evidence(plan)
    after["app_checks"][0]["observed_at"] = at(4000)
    assert "OBSERVATION_WINDOW_TOO_LONG" in codes(result(plan, before, after)[1])


def test_policy_not_read_back_is_unknown(plan):
    before, after = good_evidence(plan)
    del after["policy_readback"]
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["policy:readback"]["code"] == "POLICY_NOT_READ"


def test_policy_changed_during_observation_is_unknown(plan):
    before, after = good_evidence(plan)
    after["policy_readback"]["end"] = []
    assert result(plan, before, after)[1]["policy:readback"]["code"] == "POLICY_CHANGED_DURING_OBSERVATION"


def test_same_named_policy_in_another_namespace_is_not_the_plan(plan):
    before, after = good_evidence(plan)
    for side in ("start", "end"):
        for manifest in after["policy_readback"][side]:
            manifest["metadata"]["namespace"] = "somewhere-else"
    assert result(plan, before, after)[1]["policy:readback"]["code"] == "POLICY_NOT_READ"


def test_policy_differing_from_plan_is_unknown(plan):
    before, after = good_evidence(plan)
    for side in ("start", "end"):
        after["policy_readback"][side] = []
    assert result(plan, before, after)[1]["policy:readback"]["code"] == "POLICY_NOT_APPLIED"


def test_wider_policy_found_after_block_fails(plan):
    """정책 객체가 맞아도 합집합으로 허용이 남으면 차단이 아니다."""
    before, after = good_evidence(plan)
    leak = network_policy("leak", {"matchLabels": {"app": "demo-chat"}}, egress=[{}])
    for side in ("start", "end"):
        after["policy_readback"][side].append(copy.deepcopy(leak))
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["policy:readback"]["code"] == "POLICY_WIDER_THAN_PLAN"


def test_peer_allowed_only_for_another_workload_counts_as_wider(plan):
    """풀러에게만 허용한 DB 직접 연결을 앱에도 주면 계획보다 넓다."""
    before, after = good_evidence(plan)
    pooler_rule = next(
        r for m in plan["manifests"] if m["metadata"]["name"] == "exit-drill-egress-pooler"
        for r in m["spec"]["egress"] if "cnpg.io/cluster" in str(r["to"])
    )
    leak = network_policy("app-direct-db", {"matchLabels": {"app": "demo-chat"}}, egress=[copy.deepcopy(pooler_rule)])
    for side in ("start", "end"):
        after["policy_readback"][side].append(copy.deepcopy(leak))
    assert result(plan, before, after)[1]["policy:readback"]["code"] == "POLICY_WIDER_THAN_PLAN"


def test_baseline_taken_with_policy_in_place_is_unknown(plan):
    before, after = good_evidence(plan)
    for side in ("start", "end"):
        before["policy_readback"][side] = copy.deepcopy(plan["manifests"])
    assert result(plan, before, after)[1]["policy:baseline"]["code"] == "BASELINE_NOT_CLEAN"


def test_baseline_readback_missing_is_unknown(plan):
    before, after = good_evidence(plan)
    del before["policy_readback"]
    assert result(plan, before, after)[1]["policy:baseline"]["code"] == "POLICY_NOT_READ"


# ---- 전제와 범위 ----


def test_pod_outside_the_plan_is_unknown(plan):
    """계획을 만든 뒤 새로 뜬 Pod는 차단 범위 밖이라 AWS에 닿는지 모른다."""
    before, after = good_evidence(plan)
    after["unclassified_pods"] = ["new-worker"]
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["scope:pods"]["code"] == "POD_OUTSIDE_PLAN"


def test_host_network_pod_fails(plan):
    """계획을 만든 뒤 hostNetwork로 뜬 Pod에는 정책이 안 걸린다."""
    before, after = good_evidence(plan)
    after["pods"]["app"][0]["host_network"] = True
    verdict, checks = result(plan, before, after)
    assert verdict == "FAIL"
    assert checks["scope:host-network"]["code"] == "HOST_NETWORK_POD"


def test_unknown_host_network_is_unknown(plan):
    before, after = good_evidence(plan)
    del after["pods"]["app"][0]["host_network"]
    assert result(plan, before, after)[1]["scope:host-network"]["code"] == "HOST_NETWORK_UNKNOWN"


def test_pod_without_labels_cannot_rule_out_wider_policy(plan):
    before, after = good_evidence(plan)
    leak = network_policy("leak", {"matchLabels": {"app": "demo-chat"}}, egress=[{"to": [{"ipBlock": {"cidr": "52.0.0.0/8"}}]}])
    for side in ("start", "end"):
        after["policy_readback"][side].append(copy.deepcopy(leak))
    del after["pods"]["app"][0]["labels"]
    assert result(plan, before, after)[1]["policy:readback"]["code"] == "POLICY_NOT_READ"


def test_garbage_pod_entry_is_not_dropped(plan):
    before, after = good_evidence(plan)
    after["pods"]["app"].append("not a pod")
    assert result(plan, before, after)[1]["restart:app"]["code"] == "NOT_OBSERVED"


def test_baseline_from_long_ago_is_unknown(plan):
    before, after = good_evidence(plan)
    before["probes"][0]["observed_at"] = at(-90 * 24 * 3600, APPLIED)
    assert "OBSERVATION_WINDOW_TOO_LONG" in codes(result(plan, before, after)[1])


def test_stream_samples_are_ordered_by_time_not_by_text(plan):
    """시간대가 섞이면 글자 순서와 시간 순서가 다르다. 뒤로 간 LSN을 통과시키면 안 된다."""
    before, after = good_evidence(plan)
    earlier, later = RESTARTED + timedelta(seconds=40), RESTARTED + timedelta(seconds=50)
    after["replication"][0].update(lsn="0/2800", observed_at=earlier.astimezone(timezone(timedelta(hours=9))).isoformat())
    after["replication"][1].update(lsn="0/2000", observed_at=later.isoformat())
    assert result(plan, before, after)[1]["keep:replication-reverse:stream"]["code"] == "REPLICATION_IDLE"


@pytest.mark.parametrize("damage", [
    lambda a: a["probes"][0].update(state=["connected"]),
    lambda a: a["probes"][0].update(pod_uid=["x"]),
    lambda a: a["pods"]["app"][0].update(uid=["x"]),
    lambda a: a["app_checks"][0].update(id=["x"]),
    lambda a: a["replication"][0].update(route_id={"a": 1}),
])
def test_values_of_the_wrong_type_are_unknown_not_a_crash(plan, damage):
    """예외로 죽으면 종료 코드 1이라 FAIL로 읽힌다. 모르는 것은 UNKNOWN이다."""
    before, after = good_evidence(plan)
    damage(after)
    assert judge(plan, before, after)["verdict"] == "UNKNOWN"


def test_pod_scope_not_read_is_unknown(plan):
    before, after = good_evidence(plan)
    del after["unclassified_pods"]
    assert result(plan, before, after)[1]["scope:pods"]["code"] == "POD_SCOPE_UNKNOWN"


@pytest.mark.parametrize("cutover", [{"writer": "source"}, {"writer": "unknown"}, {}, None])
def test_cutover_not_confirmed_is_unknown(plan, cutover):
    """쓰기가 아직 출발지면 이 시험의 의미가 없다. 실패가 아니라 판정 불가다."""
    before, after = good_evidence(plan)
    after["cutover"] = cutover
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["cutover:writer"]["code"] == "CUTOVER_NOT_CONFIRMED"


def test_dependency_that_cannot_be_probed_is_unknown():
    doc = inventory_doc()
    doc["items"].append({
        **doc["items"][0], "id": "item-006", "role": "unknown", "service": "google.cloud",
        "grade": "unknown", "agent_needed": True,
    })
    plan = make_plan(inventory=redigest(doc))
    before, after = good_evidence(plan)
    verdict, checks = result(plan, before, after)
    assert verdict == "UNKNOWN"
    assert checks["coverage:item-006"]["code"] == "UNMAPPED_DEPENDENCY"


def test_every_code_has_one_meaning():
    """같은 코드가 어떤 때는 FAIL, 어떤 때는 UNKNOWN이면 판정 결과를 믿고 읽을 수 없다."""
    source = (Path(verdict_module.__file__)).read_text(encoding="utf-8")
    seen: dict[str, set[str]] = {}
    for level, code in re.findall(r'Check\([^,]+, (PASS|FAIL|UNKNOWN), "([A-Z_]+)"\)', source):
        seen.setdefault(code, set()).add(level)
    assert len(seen) > 30
    assert {code: levels for code, levels in seen.items() if len(levels) > 1} == {}


def test_plan_without_any_required_check_never_passes(plan):
    """판정할 것이 없으면 모두 통과가 아니라 UNKNOWN이다."""
    empty = {**plan, "decisions": [], "new_connection_checks": [], "workloads": {}, "manifests": []}
    empty["digest"] = compute_digest(empty)
    before, after = good_evidence(plan)
    before["plan_digest"] = after["plan_digest"] = empty["digest"]
    verdict, checks = result(empty, before, after)
    assert verdict == "UNKNOWN"
    assert "NOTHING_TO_JUDGE" in codes(checks)


def test_hand_edited_plan_is_unknown(plan):
    """차단 항목을 지운 계획으로는 통과할 수 없다."""
    before, after = good_evidence(plan)
    edited = {**plan, "decisions": [d for d in plan["decisions"] if d["route_id"] != "aws-s3"]}
    verdict, checks = result(edited, before, after)
    assert verdict == "UNKNOWN"
    assert "PLAN_TAMPERED" in codes(checks)


@pytest.mark.parametrize("bad", [None, [], "text"])
def test_garbage_observation_is_unknown_not_a_crash(plan, bad):
    before, _ = good_evidence(plan)
    assert judge(plan, before, bad)["verdict"] == "UNKNOWN"


def test_details_do_not_echo_observed_values(plan):
    """판정 결과가 관측값을 그대로 옮기지 않는다. 주소나 시크릿이 섞여 들어올 수 있다."""
    before, after = good_evidence(plan)
    rows(after, "aws-s3", "app")[0]["state"] = "secret-value-123"
    report = judge(plan, before, after)
    assert "secret-value-123" not in str(report)
