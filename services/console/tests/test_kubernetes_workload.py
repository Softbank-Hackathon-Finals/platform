import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bravo_console.kubernetes_workload import ClusterRegistration, CollectionError, collect
from bravo_console.observed_workload import ObservedWorkloads

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
CLUSTER = "00000000-0000-4000-8000-000000000001"


def registration():
    return ClusterRegistration.parse(
        dict(
            environment_id="registered-target",
            lane="target",
            cluster_uid=CLUSTER,
            namespace="target-dev",
            services=[
                dict(name="chat", label="채팅 API", kind="app", probe_port=8080),
                dict(name="database", label="DB", kind="database", probe_port=None),
            ],
        )
    )


class Reader:
    def __init__(self):
        self.uid = CLUSTER
        self.probe = True
        self.calls = []
        self.services = [
            dict(
                metadata=dict(name="chat", namespace="target-dev", uid="service-chat"),
                spec=dict(selector={"app": "chat"}),
            ),
            dict(
                metadata=dict(name="database", namespace="target-dev", uid="service-db"),
                spec=dict(selector={"app": "db"}),
            ),
        ]
        self.pods = [
            dict(
                metadata=dict(
                    name="chat-current",
                    namespace="target-dev",
                    uid="pod-chat",
                    labels={"app": "chat"},
                ),
                spec=dict(
                    containers=[
                        dict(
                            name="web",
                            env=[
                                dict(name="DB_HOST", value="database.target-dev.svc"),
                                dict(name="PASSWORD", value="must-not-leave-collector"),
                            ],
                        )
                    ]
                ),
                status=dict(
                    containerStatuses=[
                        dict(
                            name="web", containerID="fixture-runtime-1", ready=True, restartCount=0
                        )
                    ]
                ),
            )
        ]

    def get(self, kind, name=None, **kwargs):
        self.calls.append((kind, name))
        if kind == "namespace":
            return {"metadata": {"uid": self.uid}}
        return {
            "items": copy.deepcopy(
                {"services": self.services, "pods": self.pods, "networkpolicies": []}[kind]
            )
        }

    def health(self, r, service):
        return self.probe


@pytest.fixture
def case():
    reader = Reader()
    r = registration()
    clock = [NOW]
    source = ObservedWorkloads(
        application_id="registered-chat",
        source_revision="a" * 40,
        registrations=[r],
        collect=lambda selected: collect(reader, selected, source_revision="a" * 40, now=clock[0]),
        clock=lambda: clock[0],
    )
    source.refresh(r)
    return reader, r, source, clock


def test_real_selector_and_container_host_reference_do_not_claim_traffic_or_security(case):
    reader, r, source, _ = case
    result = source.read()
    assert result["environments"][0]["status"] == "CURRENT"
    assert result["plan_connected"] is False and result["authorizes_execution"] is False
    assert "plan_id" not in result
    graph = result["graph"]
    assert len(graph["nodes"]) == 5  # services, pod, container, HTTP reader
    assert all(node["security"] == "unknown" for node in graph["nodes"])
    assert not graph["responses"] and not graph["events"]
    assert any(edge["label"] == "컨테이너 설정 참조" for edge in graph["edges"])
    assert all(
        edge["state"] == "unknown" for edge in graph["edges"] if edge["kind"] == "dependency"
    )
    assert [e["state"] for e in graph["edges"] if e["kind"] == "http"] == ["observed"]
    assert "must-not-leave-collector" not in str(result)
    assert all(
        kind in {"namespace", "services", "pods", "networkpolicies"} for kind, _ in reader.calls
    )


def test_ready_pod_never_inherits_a_service_http_probe(case):
    _, _, source, _ = case
    nodes = source.read()["graph"]["nodes"]
    ready = [n for n in nodes if n["health"] == "ready"]
    assert len(ready) == 1 and ready[0]["resource_uid"] == "service-chat"
    assert next(n for n in nodes if n["resource_uid"] == "pod-chat")["health"] == "unknown"


def test_wrong_cluster_has_no_output_and_cannot_call_other_namespace():
    reader = Reader()
    reader.uid = "other"
    with pytest.raises(CollectionError, match="CLUSTER_IDENTITY_MISMATCH"):
        collect(reader, registration(), source_revision="a" * 40, now=NOW)
    assert reader.calls == [("namespace", "kube-system")]


def test_service_replacement_during_probe_is_refused():
    reader = Reader()

    def replacing(r, entry):
        reader.services[0]["metadata"]["uid"] = "replacement-service"
        return True

    reader.health = replacing
    with pytest.raises(CollectionError, match="SERVICE_CHANGED_DURING_READ"):
        collect(reader, registration(), source_revision="a" * 40, now=NOW)


def test_collection_failure_clears_http_claim_without_renewing_observation(case):
    reader, r, source, clock = case
    old = source.read()
    reader.uid = "changed"
    clock[0] += timedelta(seconds=5)
    source.refresh(r)
    result = source.read()
    assert result["environments"][0]["status"] == "STALE"
    assert result["graph"]["observed_at"] == old["graph"]["observed_at"]
    assert not result["graph"]["evidence"]
    assert all(n["health"] == "unknown" for n in result["graph"]["nodes"])
    assert all(f["ready"] is None for f in result["graph"]["facets"].values())


def test_clock_expiry_clears_ready_and_probes_without_a_new_poll(case):
    _, _, source, clock = case
    clock[0] += timedelta(seconds=91)
    result = source.read()
    assert result["environments"][0]["status"] == "STALE"
    assert not result["graph"]["evidence"]
    assert all(n["health"] == "unknown" for n in result["graph"]["nodes"])


def test_registered_reference_is_not_just_a_hash(case):
    _, r, source, _ = case
    plan = dict(
        workload_id="registered-chat",
        source_environment_id="source",
        target_environment_id=r.environment_id,
        plan_id="real-plan",
        plan_digest="b" * 64,
        version=1,
    )
    issued = source.load({}, plan)
    assert source.authenticate({}, plan, issued)
    assert not source.authenticate({}, plan, replace(issued))
    with pytest.raises(ValueError, match="observation_plan_mismatch"):
        source.load({}, plan | {"workload_id": "other"})
    source.refresh(r)
    assert not source.authenticate({}, plan, issued)


@pytest.mark.parametrize(
    "field,value", [("security", "isolated"), ("security", "clear"), ("health", "ready")]
)
def test_inventory_cannot_claim_security_or_copy_readiness_into_http_health(case, field, value):
    reader, r, source, clock = case

    def forged(registration):
        document = collect(reader, registration, source_revision="a" * 40, now=clock[0])
        document["nodes"][0][field] = value
        return document

    source.collect = forged
    source.refresh(r)
    result = source.read()
    assert result["environments"][0]["status"] == "STALE"
    assert not result["graph"]["evidence"]
    assert all(
        node["security"] == "unknown" and node["health"] == "unknown"
        for node in result["graph"]["nodes"]
    )


def test_native_plan_must_keep_registered_source_target_direction(case):
    _, r, source, _ = case
    plan = dict(
        workload_id="registered-chat",
        source_environment_id=r.environment_id,
        target_environment_id="other",
        plan_id="real-plan",
        plan_digest="b" * 64,
        version=1,
    )
    with pytest.raises(ValueError, match="observation_plan_mismatch"):
        source.load({}, plan)


def test_container_ready_does_not_override_a_failed_pod_readiness_gate(case):
    reader, r, source, clock = case
    reader.pods[0]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    source.refresh(r)
    doc = source.read()
    pod = next(n for n in doc["graph"]["nodes"] if n["resource_uid"] == "pod-chat")
    assert doc["graph"]["facets"][pod["id"]]["ready"] is False
    reader.pods[0]["status"]["conditions"] = []
    source.refresh(r)
    assert source.read()["graph"]["facets"][pod["id"]]["ready"] is None


def test_a_missing_registered_service_is_counted_instead_of_silent_success(case):
    reader, r, source, _ = case
    reader.services.pop()
    source.refresh(r)
    doc = source.read()
    assert doc["environments"][0]["registered_service_count"] == 2
    assert doc["environments"][0]["observed_service_count"] == 1


@pytest.mark.parametrize(
    "args",
    [
        ["exec", "pod", "--", "anything"],
        ["apply", "-f", "anything"],
        ["delete", "pod", "any"],
        ["get", "secrets", "--namespace", "target-dev", "-o", "json"],
        ["logs", "pod"],
        ["get", "--raw", "/api/v1/namespaces/target-dev/services/admin:8080/proxy/admin"],
        ["get", "pods", "--all-namespaces", "-o", "json"],
    ],
)
def test_cli_guard_refuses_effects_secrets_and_arbitrary_proxy_paths_before_subprocess(
    args, monkeypatch
):
    from bravo_console.kubernetes_workload import KubectlReader

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unapproved subprocess invoked")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    with pytest.raises(CollectionError, match="READ_SCOPE_INVALID"):
        KubectlReader.run(object(), args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cluster_uid", "00000000-0000-4000-8000-000000000002"),
        ("registration_digest", "f" * 64),
        ("producer_revision", "b" * 40),
        ("producer_module_sha256", "0" * 64),
    ],
)
def test_registered_cluster_scope_and_exact_producer_are_required(case, field, value):
    reader, r, source, clock = case

    def mismatched(selected):
        result = collect(reader, selected, source_revision="a" * 40, now=clock[0])
        result[field] = value
        return result

    source.collect = mismatched
    source.refresh(r)
    result = source.read()
    assert result["environments"][0]["status"] == "STALE"
    assert not result["graph"]["evidence"]


def test_registration_services_cannot_be_mutated_after_registration():
    r = registration()
    with pytest.raises(TypeError):
        r.services[0]["name"] = "other-app"
