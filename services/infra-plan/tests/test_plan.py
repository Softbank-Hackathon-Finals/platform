"""T44 stack loading and offline plans (ADR-0032)."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from bravo_infra_plan import HelmChartRenderer, Planner, StackError, load_stack, parse_stack
from bravo_infra_plan.cli import main

REPO = Path(__file__).resolve().parents[3]
STACK = REPO / "infra/stacks/target-onprem/stack.json"
REQUEST = {"stack": "target-onprem", "environment_id": "env-onprem",
           "params": {"node_name": "bravo-target-1", "arch": "arm64", "api_service_ip": "10.43.0.1",
                      "api_endpoint_ip": "192.168.64.10", "kube_version": "v1.35.9"}}
TARGET = {"cluster_uid": "cluster-1", "node_uid": "node-1"}
REV, BASE = "1" * 40, "2" * 40


class FakeCharts:
    """Stands in for `helm template`; the real path is HelmChartRenderer."""

    def __init__(self):
        self.calls = []

    def template(self, chart, *, release, namespace, values_path, include_crds, kube_version):
        self.calls.append((chart["file"], release, namespace, json.loads(Path(values_path).read_text())
                           if str(values_path).endswith(".json") else "file", include_crds, kube_version))
        if chart["file"].startswith("openbao"):  # real chart output, so finalize-bao checks run
            return json.loads((Path(__file__).parent / "fixtures/openbao-0.30.6-chart.json").read_text())
        return [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": release, "namespace": namespace}}]


def plan(request=REQUEST, stack=None, **kw):
    stack = stack or load_stack(STACK)
    return Planner(stack, REPO, chart_renderer=FakeCharts()).plan(
        request, stack_revision=kw.get("rev", REV), target=kw.get("target", TARGET), base_commit=kw.get("base", BASE))


def test_target_stack_runs_four_lines_in_parallel_after_base():
    stack = load_stack(STACK)
    levels = {step: n for n, level in enumerate(stack.levels) for step in level}
    assert levels["base"] < levels["base-sync"] < levels["cnpg"]
    assert levels["cnpg"] == levels["eso"] == levels["tailscale-oauth"] == levels["db-secrets"]
    assert levels["app"] > max(levels["migration-complete"], levels["objects-sync"], levels["delivery-ready"])
    assert stack.levels[-1] == ("verify",)
    kinds = {s["kind"] for s in stack.document["steps"]}
    assert kinds == {"probe", "operator_action", "helm_release", "gitops_publish", "gitops_sync",
                     "render_apply", "secret_generate", "wait"}


def test_real_renderers_give_a_deterministic_plan_and_split_the_namespace():
    first, second = plan(), plan()
    assert first.plan_digest == second.plan_digest and first.files == second.files
    base = {name: json.loads(text)["items"] for name, text in first.files["base"].items()}
    assert [i["kind"] for i in base["base/target-namespace.json"]] == ["Namespace"]
    database = json.loads(first.files["database"]["database/database.json"])["items"]
    assert "Namespace" not in {i["kind"] for i in database} and "Cluster" in {i["kind"] for i in database}
    assert set(first.files["migration"]) == {"migration.json"}  # render_apply: not in the Git tree
    tree = {f for step in first.document["steps"] if step["kind"] == "gitops_publish" for f in step["files"]}
    assert "migration.json" not in tree and "openbao/server.json" in tree


def test_every_input_moves_the_plan_digest():
    digest = plan().plan_digest
    changed = copy.deepcopy(REQUEST)
    changed["params"]["node_name"] = "bravo-target-2"
    assert plan(changed).plan_digest != digest
    assert plan(rev="3" * 40).plan_digest != digest
    assert plan(base="4" * 40).plan_digest != digest
    assert plan(target=TARGET | {"cluster_uid": "cluster-2"}).plan_digest != digest


def test_plan_holds_no_secret_value_and_secret_steps_only_name_keys():
    result = plan()
    rendered = [item for files in result.files.values() for text in files.values()
                for item in json.loads(text)["items"]]
    assert all(not item.get("data") and not item.get("stringData") for item in rendered if item["kind"] == "Secret")
    secrets = [s for s in result.document["steps"] if s["kind"] == "secret_generate"]
    assert secrets and all("files" not in s for s in secrets)
    stack = load_stack(STACK)
    for step in stack.document["steps"]:
        for secret in step.get("secrets", []):
            for spec in secret["keys"].values():
                assert set(spec) <= {"literal", "random", "builtin"}
                assert "literal" not in spec or spec["literal"] in ("chat_app", "chat_replicator")


def test_charts_get_rendered_values_post_processing_and_the_kube_version():
    charts = FakeCharts()
    result = Planner(load_stack(STACK), REPO, chart_renderer=charts).plan(
        REQUEST, stack_revision=REV, target=TARGET, base_commit=BASE)
    by_file = {call[0]: call for call in charts.calls}
    assert by_file["openbao-0.30.6.tgz"][5] == "v1.35.9"
    assert by_file["cloudnative-pg-0.29.1.tgz"][3] == "file"  # cnpg-values.yaml from the repository
    assert by_file["tailscale-operator-1.104.4.tgz"][4] is True
    server = json.loads(result.files["openbao"]["openbao/server.json"])["items"]
    [statefulset] = [i for i in server if i["kind"] == "StatefulSet"]
    probe = statefulset["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert "-tls-skip-verify" not in json.dumps(probe)  # finalize-bao replaced the chart's probe


@pytest.mark.parametrize("change, code", [
    (lambda r: r["params"].update(node_name="Bad_Node"), "invalid_plan_request"),
    (lambda r: r["params"].update(arch="s390x"), "invalid_plan_request"),
    (lambda r: r["params"].pop("kube_version"), "invalid_plan_request"),
    (lambda r: r["params"].update(extra="x"), "invalid_plan_request"),
    (lambda r: r.update(stack="other"), "invalid_plan_request"),
])
def test_requests_outside_the_stack_schema_are_refused(change, code):
    request = copy.deepcopy(REQUEST)
    change(request)
    with pytest.raises(StackError, match=code):
        plan(request)


def test_target_identity_and_revisions_are_required():
    with pytest.raises(StackError, match="target_identity_required"):
        plan(target={"cluster_uid": "c"})
    with pytest.raises(StackError, match="git_revision_required"):
        plan(rev="main")


def _doc():
    return json.loads(STACK.read_text())


@pytest.mark.parametrize("edit, code", [
    (lambda d: d["steps"].append(copy.deepcopy(d["steps"][0])), "duplicate_step"),
    (lambda d: d["steps"][0].update(needs=["nope"]), "unknown_dependency"),
    (lambda d: d["steps"][0].update(needs=["verify"]), "stack_cycle"),
    (lambda d: d["steps"][0].update(kind="shell", command="rm -rf /"), "invalid_stack"),
    (lambda d: d["steps"][4]["renders"][0]["args"].append("${secret}"), "unknown_parameter"),
    (lambda d: d["steps"][5].update(publish="identity"), "sync_without_publish"),
    (lambda d: d["steps"][4]["renders"][0].update(run="/etc/passwd"), "invalid_stack"),
    (lambda d: d["steps"][4]["renders"][0].update(run="../outside.py"), "invalid_stack"),
])
def test_malformed_stacks_are_refused_before_rendering(edit, code):
    document = _doc()
    edit(document)
    with pytest.raises(StackError, match=code):
        parse_stack(document)


def test_a_chart_archive_with_another_hash_is_refused(tmp_path):
    (tmp_path / "openbao-0.30.6.tgz").write_bytes(b"not the pinned chart")
    chart = load_stack(STACK).document["charts"]["openbao"]
    renderer = HelmChartRenderer(tmp_path, helm="/bin/false")
    with pytest.raises(StackError, match="chart_hash_mismatch"):
        renderer.template(chart, release="r", namespace="n", values_path=tmp_path / "v", include_crds=False,
                          kube_version="v1.35.9")
    with pytest.raises(StackError, match="chart_unavailable"):
        renderer.template(load_stack(STACK).document["charts"]["external-secrets"], release="r", namespace="n",
                          values_path=tmp_path / "v", include_crds=False, kube_version="v1.35.9")
    assert hashlib.sha256(b"not the pinned chart").hexdigest() != chart["sha256"]


def test_cli_check_and_request_schema(capsys):
    assert main(["check", str(STACK)]) == 0
    assert capsys.readouterr().out.splitlines()[0].split() == ["1", "identity"]
    assert main(["request-schema", str(STACK)]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["properties"]["params"]["required"] == sorted(REQUEST["params"])
