"""T45 stack loading and offline plans (ADR-0033)."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from bravo_infra_plan import HelmChartRenderer, Planner, StackError, load_stack, parse_stack
from bravo_infra_plan.cli import main

REPO = Path(__file__).resolve().parents[3]
STACK = REPO / "infra/stacks/target-onprem/stack.json"
REQUEST = {"stack": "target-onprem", "environment_id": "target-dev",
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
        if chart["file"].startswith("openbao"):
            # Real `helm template` output so finalize-bao's checks run. Only the container HOME path is
            # rewritten (to /openbao/home): the public-file check reads the original as a personal one.
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
    assert kinds == {"probe", "operator_action", "gitops_publish", "gitops_sync", "render_apply",
                     "secret_generate", "wait"}  # helm_release exists but this stack installs no chart itself


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
                assert set(spec) <= {"literal", "random"}
                assert "literal" not in spec or spec["literal"] in ("chat_app", "chat_replicator")


def test_charts_get_rendered_values_post_processing_and_the_kube_version():
    charts = FakeCharts()
    result = Planner(load_stack(STACK), REPO, chart_renderer=charts).plan(
        REQUEST, stack_revision=REV, target=TARGET, base_commit=BASE)
    by_file = {call[0]: call for call in charts.calls}
    assert by_file["openbao-0.30.6.tgz"][5] == "v1.35.9"
    assert by_file["cloudnative-pg-0.29.1.tgz"][3] == "file"  # cnpg-values.yaml from the repository
    assert by_file["tailscale-operator-1.104.4.tgz"][4] is True
    cnpg = json.loads(result.files["cnpg"]["cnpg/operator.json"])["items"]
    assert cnpg[0]["kind"] == "Namespace" and cnpg[0]["metadata"]["name"] == "cnpg-system"
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
        plan(target={})
    with pytest.raises(StackError, match="target_identity_required"):
        plan(target={"cluster_uid": ""})
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
    # A condition must name a closed parameter and only its values; a default must be one of them.
    (lambda d: d["steps"][1].update(when={"param": "node_name", "in": ["x"]}), "invalid_step_condition"),
    (lambda d: d["steps"][1].update(when={"param": "tailnet", "in": ["maybe"]}), "invalid_step_condition"),
    (lambda d: d["params"]["tailnet"].update(default="maybe"), "invalid_parameter_default"),
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


def _desired_world(tmp_path, environments):
    """A minimal repository with only T44 desired-state files and one operator step that reads them."""
    for name, state in environments.items():
        path = tmp_path / "infra/iac/environments" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "environment": name, "modules": [
            {"id": "target-object-exposure", "version": 1, "params": state}]}))
    stack = parse_stack({"schema_version": 1, "stack": "desired-test", "charts": {},
                         "params": {"node_name": {"description": "n"}}, "steps": [
        {"id": "object-secrets", "kind": "operator_action", "owner": "T9", "description": "d",
         "instruction": "i", "confirm": "seaweedfs-auth",
         "desired": {"file": "infra/iac/environments/{environment_id}.json", "module": "target-object-exposure",
                     "params": ["state", "bucket", "demo_run_id"]}}]})
    return lambda env: Planner(stack, tmp_path).plan(
        {"stack": "desired-test", "environment_id": env, "params": {"node_name": "n"}},
        stack_revision=REV, target=TARGET, base_commit=BASE)


def test_each_environment_gets_its_own_desired_state_and_a_missing_one_refuses(tmp_path):
    plan_for = _desired_world(tmp_path, {
        "target-dev": {"state": "open-demo", "bucket": "demo-chat-attachments", "demo_run_id": "demo-2026-11-08"},
        "target-e2e": {"state": "closed", "bucket": "demo-chat-attachments"}})
    assert plan_for("target-dev").document["steps"][0]["desired"]["state"] == "open-demo"
    assert plan_for("target-e2e").document["steps"][0]["desired"] == {"state": "closed",
                                                                       "bucket": "demo-chat-attachments"}
    with pytest.raises(StackError, match="desired_state_unavailable"):  # never borrow target-dev's open-demo
        plan_for("target-new")


def test_a_target_without_a_tailnet_omits_the_tailscale_line_and_the_digest_binds_it():
    joined = plan()
    assert joined.document["request"]["params"]["tailnet"] == "join"  # the default is written into the plan
    assert not any(s.get("omitted") for s in joined.document["steps"])
    isolated_request = copy.deepcopy(REQUEST)
    isolated_request["params"]["tailnet"] = "none"
    charts = FakeCharts()
    isolated = Planner(load_stack(STACK), REPO, chart_renderer=charts).plan(
        isolated_request, stack_revision=REV, target=TARGET, base_commit=BASE)
    omitted = {s["id"] for s in isolated.document["steps"] if s.get("omitted")}
    assert omitted == {"tailscale-oauth", "tailscale", "tailscale-sync"}
    assert not any(call[0].startswith("tailscale") for call in charts.calls)  # nothing rendered for it
    assert not any(name.startswith("tailscale/") for name in isolated.document["steps"][0].get("files", {}))
    assert isolated.plan_digest != joined.plan_digest


def test_the_offline_app_image_step_exists_only_for_arm64_nodes():
    stack = load_stack(STACK)
    assert stack.applies("app-image", {"arch": "arm64"})
    assert not stack.applies("app-image", {"arch": "amd64"})  # amd64 pulls the pinned registry image
    assert "app-image" not in {s["id"] for s in plan().document["steps"] if s.get("omitted")}


def test_a_wif_target_joins_with_its_federated_identity_and_no_person_or_secret():
    request = copy.deepcopy(REQUEST)
    request["params"] |= {"tailnet": "wif", "tailnet_env": "t49", "tailscale_client_id": "kFixture123",
                          "tailscale_audience": "api.tailscale.com/kFixture123"}
    charts = FakeCharts()
    wif = Planner(load_stack(STACK), REPO, chart_renderer=charts).plan(
        request, stack_revision=REV, target=TARGET, base_commit=BASE)
    assert {s["id"] for s in wif.document["steps"] if s.get("omitted")} == {"tailscale-oauth"}  # no human step
    [values] = [call[3] for call in charts.calls if call[1] == "tailscale-operator"]
    assert values["oauth"] == {"clientId": "kFixture123", "audience": "api.tailscale.com/kFixture123"}
    assert values["operatorConfig"]["defaultTags"] == ["tag:bravo-t49-operator"]
    request["params"]["tailscale_audience"] = "unset"
    with pytest.raises(StackError):  # wif without the identity never renders a secret-shaped operator
        Planner(load_stack(STACK), REPO, chart_renderer=FakeCharts()).plan(
            request, stack_revision=REV, target=TARGET, base_commit=BASE)
