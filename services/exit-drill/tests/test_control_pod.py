"""대조군 Pod. 계획을 만들려면 먼저 떠 있어야 하고, 차단되지 않아야 하며, 비밀을 갖지 않는다."""

import pytest
from helpers import healthy_pods, inventory_doc, snapshot_doc, spec_doc

from bravo_exit_drill.cli import main
from bravo_exit_drill.cluster import parse_snapshot
from bravo_exit_drill.control_pod import ControlPodError, render_control_pod
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.policy import build_plan
from bravo_exit_drill.spec import parse_spec

IMAGE = "ghcr.io/softbank-hackathon-finals/demo-chat@sha256:" + "a" * 64


def rendered() -> dict:
    return render_control_pod(parse_spec(spec_doc()), IMAGE)


def test_labels_come_from_the_spec_and_can_be_rolled_back_with_the_policies():
    labels = rendered()["metadata"]["labels"]
    assert labels["bravo-exit-drill-control"] == "true"
    assert labels["app.kubernetes.io/part-of"] == "bravo-exit-drill"
    assert labels["component"] == "target"


def test_control_pod_is_accepted_by_the_planner_and_no_policy_selects_it():
    pods = [p for p in healthy_pods() if p["metadata"]["name"] != "control-1"]
    pod = {**rendered(), "status": {"phase": "Running"}}
    plan = build_plan(
        parse_spec(spec_doc()),
        parse_inventory(inventory_doc(), candidate="onprem-demo"),
        parse_snapshot(snapshot_doc(pods=[*pods, pod]), namespace="target-dev"),
    )
    assert plan["manifests"]


def test_meets_the_restricted_pod_security_of_target_dev():
    spec = rendered()["spec"]
    security = spec["securityContext"]
    assert security["runAsNonRoot"] is True and security["seccompProfile"] == {"type": "RuntimeDefault"}
    container = spec["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}


def test_holds_no_credentials():
    pod = rendered()
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert "env" not in pod["spec"]["containers"][0]
    assert "volumes" not in pod["spec"]


def test_uses_only_an_image_already_on_the_machine():
    container = rendered()["spec"]["containers"][0]
    assert container["image"] == IMAGE
    assert container["imagePullPolicy"] == "Never"


def test_does_not_outlive_a_forgotten_drill():
    assert rendered()["spec"]["activeDeadlineSeconds"] > 0


@pytest.mark.parametrize("image", ["", "demo-chat:latest", "ghcr.io/x/demo-chat", "ghcr.io/x/demo-chat@sha256:abc", 1, None])
def test_image_must_be_pinned_by_digest(image):
    with pytest.raises(ControlPodError):
        render_control_pod(parse_spec(spec_doc()), image)


def test_spec_without_control_stops():
    doc = spec_doc()
    del doc["workloads"]["control"]
    with pytest.raises(ControlPodError):
        render_control_pod(parse_spec(doc), IMAGE)


def test_command_line_prints_the_manifest(tmp_path, capsys):
    from helpers import PROFILE

    assert main(["control-pod", "--spec", str(PROFILE), "--image", IMAGE]) == 0
    assert '"kind": "Pod"' in capsys.readouterr().out
    assert main(["control-pod", "--spec", str(PROFILE), "--image", "demo-chat:latest"]) == 2
