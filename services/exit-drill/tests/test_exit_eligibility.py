"""출구 시험 자격. AWS 호출이 다른 곳으로 돌려진 Pod는 AWS 차단을 잰 것이 아니므로 PASS를 낼 수 없다.

T30의 개발 측정용 앱은 같은 Pod의 호환 컨테이너가 Secrets Manager 호출에 응답한다(ADR-0017).
AWS를 막아도 앱이 멀쩡해 보이므로, 이 시험이 막지 않으면 거짓 PASS가 나온다.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import inventory_doc
from test_verdict import good_evidence, make_plan

from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.observation import normalize_pods
from bravo_exit_drill.residual_map import build_map
from bravo_exit_drill.verdict import exit_code, judge

ANNOTATION = "bravo.t30/not-exit-evidence"
RENDERER = Path(__file__).resolve().parents[3] / "infra/envs/target-workloads/application.py"
NOT_EVIDENCE, REDIRECTED = "NOT_EXIT_EVIDENCE", "AWS_ENDPOINT_REDIRECTED"


@pytest.fixture
def plan():
    return make_plan()


def kube_pod(*, annotations=None, env=(), init_env=(), uid="a1"):
    metadata = {"name": uid, "uid": uid, "namespace": "target-dev", "labels": {"app": "demo-chat"}}
    if annotations is not None:
        metadata["annotations"] = annotations
    spec = {"containers": [{"name": "app", "env": [{"name": n, "value": "x"} for n in env]}]}
    if init_env:
        spec["initContainers"] = [{"name": "sidecar", "restartPolicy": "Always",
                                   "env": [{"name": n, "value": "x"} for n in init_env]}]
    return {"kind": "Pod", "metadata": metadata, "spec": spec, "status": {"phase": "Running"}}


def blockers(pod, plan):
    return normalize_pods({"kind": "List", "items": [pod]}, plan)["app"][0]["exit_blockers"]


def judged(plan, *, before=None, after=None):
    good_before, good_after = good_evidence(plan)
    for observation, value in ((good_before, before), (good_after, after)):
        if value != "unchanged":
            observation["pods"]["app"][0]["exit_blockers"] = value
    return judge(plan, good_before, good_after)


def code(report, workload="app"):
    return next(c for c in report["checks"] if c["id"] == f"eligibility:{workload}")


# --- 수집: 실제 Pod에서 읽는다


def test_pod_without_marker_or_redirect_has_no_blockers(plan):
    assert blockers(kube_pod(env=["S3_ENDPOINT_URL", "DB_HOST"]), plan) == []


def test_empty_annotations_are_not_a_marker(plan):
    assert blockers(kube_pod(annotations={"other": "x"}), plan) == []


@pytest.mark.parametrize("value", ["secrets-manager-compat-sidecar", ""])
def test_marker_blocks_whatever_its_value(plan, value):
    assert blockers(kube_pod(annotations={ANNOTATION: value}), plan) == [NOT_EVIDENCE]


@pytest.mark.parametrize("name", ["AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_SECRETS_MANAGER", "AWS_ENDPOINT_URL_S3"])
def test_redirected_aws_endpoint_blocks_in_main_container(plan, name):
    assert blockers(kube_pod(env=[name]), plan) == [REDIRECTED]


def test_redirect_in_a_sidecar_blocks_too(plan):
    assert blockers(kube_pod(init_env=["AWS_ENDPOINT_URL_SECRETS_MANAGER"]), plan) == [REDIRECTED]


def test_both_reasons_are_reported(plan):
    pod = kube_pod(annotations={ANNOTATION: "x"}, env=["AWS_ENDPOINT_URL_SECRETS_MANAGER"])
    assert blockers(pod, plan) == [NOT_EVIDENCE, REDIRECTED]


def damage(pod, how):
    if how == "annotations":
        pod["metadata"]["annotations"] = ["not", "a", "map"]
    elif how == "spec":
        del pod["spec"]
    elif how == "containers":
        del pod["spec"]["containers"]
    elif how == "no-containers":
        pod["spec"]["containers"] = []
    elif how == "init":
        pod["spec"]["initContainers"] = "broken"
    elif how == "container":
        pod["spec"]["containers"] = ["broken"]
    elif how == "env":
        pod["spec"]["containers"][0]["env"] = "broken"
    elif how == "env-entry":
        pod["spec"]["containers"][0]["env"] = [{"value": "no name"}]
    return pod


@pytest.mark.parametrize("how", ["annotations", "spec", "containers", "no-containers", "init", "container", "env", "env-entry"])
def test_unreadable_pod_is_none_not_clean(plan, how):
    assert blockers(damage(kube_pod(), how), plan) is None


# envFrom은 Pod만 봐서는 어떤 변수가 들어오는지 모른다. 참조한 값을 새로 읽지 않고, 모른다고 둔다.
ENV_FROM_REFS = [
    {"configMapRef": {"name": "endpoints"}},
    {"secretRef": {"name": "endpoints"}},
    {"configMapRef": {"name": "endpoints"}, "prefix": "AWS_"},
]


@pytest.mark.parametrize("reference", ENV_FROM_REFS)
def test_env_from_on_a_main_container_cannot_be_read(plan, reference):
    pod = kube_pod()
    pod["spec"]["containers"][0]["envFrom"] = [reference]
    assert blockers(pod, plan) is None


@pytest.mark.parametrize("reference", ENV_FROM_REFS)
def test_env_from_on_a_sidecar_cannot_be_read(plan, reference):
    pod = kube_pod(init_env=["S3_ENDPOINT_URL"])
    pod["spec"]["initContainers"][0]["envFrom"] = [reference]
    assert blockers(pod, plan) is None


def test_env_from_beside_a_second_container_cannot_be_read(plan):
    pod = kube_pod()
    pod["spec"]["containers"].append({"name": "collector", "envFrom": [{"secretRef": {"name": "x"}}]})
    assert blockers(pod, plan) is None


def test_malformed_env_from_cannot_be_read(plan):
    pod = kube_pod()
    pod["spec"]["containers"][0]["envFrom"] = "broken"
    assert blockers(pod, plan) is None


def test_empty_env_from_names_nothing_and_stays_eligible(plan):
    pod = kube_pod()
    pod["spec"]["containers"][0]["envFrom"] = []
    assert blockers(pod, plan) == []


def test_env_from_is_not_read_even_beside_a_marker(plan):
    pod = kube_pod(annotations={ANNOTATION: "x"})
    pod["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": "endpoints"}}]
    assert blockers(pod, plan) is None


def test_endpoint_moved_into_env_from_cannot_pass(plan):
    """표지와 변수를 지우고 endpoint를 envFrom으로 옮긴 우회. 실제 RKE2에서 PASS가 재현됐던 입력이다(#210)."""
    pod = kube_pod()
    pod["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": "moved-endpoint"}}]
    found = blockers(pod, plan)
    report = judged(plan, before=found, after=found)
    assert report["verdict"] == "UNKNOWN"
    assert exit_code(report) == 3
    assert code(report)["code"] == "ELIGIBILITY_NOT_READ"


def test_removing_env_from_restores_eligibility(plan):
    pod = kube_pod()
    pod["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": "moved-endpoint"}}]
    assert judged(plan, before=blockers(pod, plan), after=blockers(pod, plan))["verdict"] == "UNKNOWN"
    del pod["spec"]["containers"][0]["envFrom"]
    assert judged(plan, before=blockers(pod, plan), after=blockers(pod, plan))["verdict"] == "PASS"


# --- 판정: 표지 있음 · 없음 · 읽기 실패 · 제거 뒤 회복


def test_clean_pods_keep_the_normal_pass(plan):
    report = judged(plan, before=[], after=[])
    assert report["verdict"] == "PASS"
    assert code(report) == {"id": "eligibility:app", "verdict": "PASS", "code": "ELIGIBLE"}


@pytest.mark.parametrize(("value", "expected"), [([NOT_EVIDENCE], NOT_EVIDENCE), ([REDIRECTED], REDIRECTED),
                                                 ([NOT_EVIDENCE, REDIRECTED], NOT_EVIDENCE)])
def test_marked_pod_cannot_pass(plan, value, expected):
    report = judged(plan, before=value, after=value)
    assert report["verdict"] == "UNKNOWN"
    assert exit_code(report) == 3
    assert code(report)["code"] == expected


@pytest.mark.parametrize("phase", ["before", "after"])
def test_either_phase_alone_disqualifies(plan, phase):
    report = judged(plan, **{"before": [], "after": [], phase: [NOT_EVIDENCE]})
    assert report["verdict"] == "UNKNOWN"
    assert code(report)["code"] == NOT_EVIDENCE


@pytest.mark.parametrize("value", [None, "unread", {}, 0, [1], [""]])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_unreadable_field_is_unknown(plan, phase, value):
    report = judged(plan, **{"before": [], "after": [], phase: value})
    assert report["verdict"] == "UNKNOWN"
    assert code(report)["code"] == "ELIGIBILITY_NOT_READ"


def test_observation_from_before_this_check_is_unknown(plan):
    before, after = good_evidence(plan)
    del after["pods"]["app"][0]["exit_blockers"]
    assert code(judge(plan, before, after))["code"] == "ELIGIBILITY_NOT_READ"


@pytest.mark.parametrize("phase", [0, 1])
def test_missing_pods_are_not_eligible(plan, phase):
    evidence = list(good_evidence(plan))
    evidence[phase]["pods"]["app"] = []
    assert code(judge(plan, *evidence))["code"] == "ELIGIBILITY_NOT_READ"


def test_unlisted_workload_is_not_eligible(plan):
    before, after = good_evidence(plan)
    del after["pods"]["app"]
    assert code(judge(plan, before, after))["code"] == "ELIGIBILITY_NOT_READ"


def test_every_app_role_workload_is_checked(plan):
    before, after = good_evidence(plan)
    after["pods"]["pooler"][0]["exit_blockers"] = [REDIRECTED]
    report = judge(plan, before, after)
    assert report["verdict"] == "UNKNOWN"
    assert code(report, "pooler")["code"] == REDIRECTED


def test_a_second_pod_cannot_hide_behind_a_clean_first_one(plan):
    before, after = good_evidence(plan)
    extra = copy.deepcopy(after["pods"]["app"][0])
    extra.update(name="app-other", uid="app-other", exit_blockers=[NOT_EVIDENCE])
    after["pods"]["app"].append(extra)
    assert code(judge(plan, before, after))["code"] == NOT_EVIDENCE


def test_evidence_of_a_failure_still_wins(plan):
    before, after = good_evidence(plan)
    for observation in (before, after):
        observation["pods"]["app"][0]["exit_blockers"] = [NOT_EVIDENCE]
    route = next(d for d in plan["decisions"] if d["action"] == "BLOCK")
    for row in after["probes"]:
        if row["route_id"] == route["route_id"] and row["vantage"] != "control":
            row["state"] = "connected"
    assert judge(plan, before, after)["verdict"] == "FAIL"


def test_removing_only_the_marker_is_not_enough(plan):
    marked = kube_pod(annotations={ANNOTATION: "x"}, init_env=["AWS_ENDPOINT_URL_SECRETS_MANAGER"])
    assert judged(plan, before=blockers(marked, plan), after=blockers(marked, plan))["verdict"] == "UNKNOWN"
    del marked["metadata"]["annotations"]
    report = judged(plan, before=blockers(marked, plan), after=blockers(marked, plan))
    assert report["verdict"] == "UNKNOWN"
    assert code(report)["code"] == REDIRECTED


def test_eligibility_returns_once_the_compatibility_setup_is_gone(plan):
    marked = kube_pod(annotations={ANNOTATION: "x"}, env=["AWS_ENDPOINT_URL_SECRETS_MANAGER"])
    assert judged(plan, before=blockers(marked, plan), after=blockers(marked, plan))["verdict"] == "UNKNOWN"
    cleaned = kube_pod(env=["S3_ENDPOINT_URL"])
    report = judged(plan, before=blockers(cleaned, plan), after=blockers(cleaned, plan))
    assert report["verdict"] == "PASS"
    assert code(report)["code"] == "ELIGIBLE"


# --- 다른 출구: 판정을 쓰는 지도도 근거로 삼지 못한다


def test_residual_map_does_not_call_a_marked_run_verified(plan):
    inventory = parse_inventory(inventory_doc(), candidate="onprem-demo")
    clean = build_map(inventory, plan, judged(plan, before=[], after=[]))
    marked = build_map(inventory, plan, judged(plan, before=[NOT_EVIDENCE], after=[NOT_EVIDENCE]))
    assert "blocked_app_ok" in clean["summary"]["status"]
    assert "blocked_app_ok" not in marked["summary"]["status"]
    assert marked["drill_verdict"] == "UNKNOWN"


# --- 실제 렌더 결과: T30이 만드는 Pod가 정말 걸리는가


def rendered_pod() -> dict:
    output = subprocess.run([sys.executable, str(RENDERER)], capture_output=True, text=True, check=True)
    deployment = next(i for i in json.loads(output.stdout)["items"] if i["kind"] == "Deployment")
    template = deployment["spec"]["template"]
    metadata = {**template["metadata"], "name": "demo-chat-1", "uid": "demo-chat-1", "namespace": "target-dev"}
    return {"kind": "Pod", "metadata": metadata, "spec": template["spec"], "status": {"phase": "Running"}}


# 변이 시험은 이 서비스만 복사하므로 저장소의 렌더러가 없다. 다른 시험이 같은 코드를 잰다.
needs_renderer = pytest.mark.skipif(not RENDERER.exists(), reason="repository renderer is not in this copy")


@needs_renderer
def test_the_rendered_compatibility_pod_is_refused(plan):
    found = blockers(rendered_pod(), plan)
    assert found == [NOT_EVIDENCE, REDIRECTED]
    assert judged(plan, before=found, after=found)["verdict"] == "UNKNOWN"


@needs_renderer
def test_the_rendered_pod_qualifies_once_the_setup_adr_names_is_removed(plan):
    pod = rendered_pod()
    del pod["metadata"]["annotations"]
    del pod["spec"]["initContainers"]
    app = pod["spec"]["containers"][0]
    app["env"] = [e for e in app["env"] if e["name"] != "AWS_ENDPOINT_URL_SECRETS_MANAGER"]
    found = blockers(pod, plan)
    assert found == []
    assert judged(plan, before=found, after=found)["verdict"] == "PASS"
