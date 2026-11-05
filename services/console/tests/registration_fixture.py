"""Local-only identities and Kubernetes probe. Actual T10 Registry/storage/API."""

from dataclasses import replace
from datetime import UTC, datetime

from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.registration import Registry
from bravo_platform_events.registration_api import install_registration_routes
from bravo_platform_events.registration_probe import EnvironmentRef, ProbeUnavailable, SourceRelease

SOURCE = dict(name="source", kind="aws", roles=["source"], connection_ref="source-reader")
TARGET = dict(name="target", kind="onprem", roles=["target"], connection_ref="target-reader")
WORKLOAD = dict(
    name="chat",
    repo_url="https://example.invalid/chat",
    app_path="app",
    iac_path="compose.yaml",
    source_environment_id="env-source",
    namespace="app-source",
)


def install(api, store, state, root):
    class Probe:
        def inspect(self, env, release=None):
            if state.get("registration_probe_denied"):
                raise ProbeUnavailable("required_read_denied")
            result = dict(
                namespace=env.namespace,
                database="chat",
                database_identity=env.database_uid,
                collector="fixture-reader",
                warnings=["write_scope_unknown"],
                read_access="verified",
            )
            if release:
                result.update(
                    rollout_identity="fixture-rs",
                    source_sha="a" * 40,
                    image="b" * 64,
                    service_account="chat",
                    repo_deploy_match=True,
                    resources=[{"kind": "Cluster", "name": "chat"}],
                )
            return result

    src = EnvironmentRef(
        "tenant-demo",
        "env-source",
        "aws",
        "app-source",
        "db",
        "source-db-id",
        "https://source.invalid",
        root / "unused-ca",
        lambda: "fixture-only",
    )
    dst = replace(
        src,
        environment_id="env-target",
        kind="onprem",
        namespace="app-target",
        database_uid="target-db-id",
    )
    release = SourceRelease(
        "tenant-demo",
        "env-source",
        WORKLOAD["repo_url"],
        root,
        "app",
        "compose.yaml",
        "a" * 40,
        "chat",
        "chat",
        "repo/chat@sha256:" + "b" * 64,
        ("sha256:" + "b" * 64,),
        "chat",
        "rs-uid",
    )
    registry = Registry(
        store,
        environments={
            ("tenant-demo", "source-reader"): src,
            ("tenant-demo", "target-reader"): dst,
        },
        releases=[release],
        can_register=lambda _actor: state["allowed"],
        probe=Probe(),
    )

    def identity(request):
        if not state["allowed"] or request.cookies.get("fixture-session") != "test-only":
            return None
        now = int(datetime.now(UTC).timestamp())
        return OperatorPrincipal(
            "fixture-operator", "tenant-demo", now - 1, now + 300, now - 1, "fixture-session"
        )

    install_registration_routes(api, registry, verify_identity=identity)
