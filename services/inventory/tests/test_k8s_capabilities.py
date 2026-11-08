"""Registration capability detection against fake Kubernetes answers (no cluster is contacted)."""

import json

import pytest

from bravo_inventory.capabilities import fit, observe, resolve
from bravo_inventory.k8s_capabilities import DETECTED, ReadRefused, _Reader, detect_capabilities


class Denied(Exception):
    """What a real client raises on 403: the detector must turn it into 'unknown'."""


def node(args):
    ann = {"rke2.io/node-args": json.dumps(args)} if args is not None else {}
    return {"metadata": {"name": "n", "annotations": ann}}


RKE2_CIS = {
    "/version": {"major": "1", "minor": "31", "gitVersion": "v1.31.4+rke2r1"},
    "/apis": {"kind": "APIGroupList", "groups": [
        {"name": "apps", "preferredVersion": {"version": "v1"}},
        {"name": "postgresql.cnpg.io", "preferredVersion": {"version": "v1"}},
        {"name": "external-secrets.io", "preferredVersion": {"version": "v1"}},
    ]},
    "/api/v1/nodes": {"kind": "NodeList", "metadata": {}, "items": [
        node(["server", "--profile", "cis", "--token", "********"]),
        node(["agent", "--profile=cis"]),
    ]},
    "/api/v1/services": {"kind": "ServiceList", "metadata": {}, "items": [
        {"metadata": {"namespace": "bravo-storage", "name": "seaweedfs-s3"}},
        {"metadata": {"namespace": "bravo-secrets", "name": "openbao"}},
    ]},
}


def fake(answers, calls=None):
    def get(path, params=None):
        if calls is not None:
            calls.append(("GET", path, params))
        if path not in answers:
            raise Denied(path)
        return answers[path]

    return get


def test_detects_all_five_from_read_only_answers():
    calls = []
    caps, paths = detect_capabilities(fake(RKE2_CIS, calls))
    assert caps["k8s_version"] == {"value": "v1.31.4+rke2r1", "source": "observed: /version"}
    assert caps["postgres_operator"] == {"value": "cnpg", "source": "observed: /apis group postgresql.cnpg.io"}
    assert caps["s3_compatible"] == {"value": "seaweedfs", "source": "observed: Service/bravo-storage/seaweedfs-s3"}
    assert caps["secret_store"] == {"value": "openbao", "source": "observed: Service/bravo-secrets/openbao"}
    assert caps["cis_profile"] == {"value": "rke2-cis", "source": "observed: Node rke2.io/node-args --profile"}
    assert caps["secrets_injection"]["value"] == "eso"
    # Only GETs, no request body is ever built, and the secret-looking token never reaches the output.
    assert {c[0] for c in calls} == {"GET"} and paths == [c[1] for c in calls]
    assert "********" not in json.dumps(caps)


def test_everything_denied_is_unknown_not_guessed():
    caps, _ = detect_capabilities(fake({}))
    assert set(caps) == set(DETECTED)
    assert all(v["value"] == "unknown" and v["source"].startswith("unknown: ") for v in caps.values())


def test_namespace_scoped_collector_sees_discovery_only():
    # A namespaced collector can read /version and /apis but not nodes or cluster-wide services.
    answers = {k: RKE2_CIS[k] for k in ("/version", "/apis")}
    caps, _ = detect_capabilities(fake(answers))
    assert caps["k8s_version"]["value"] == "v1.31.4+rke2r1" and caps["postgres_operator"]["value"] == "cnpg"
    assert caps["s3_compatible"]["value"] == "unknown" and caps["cis_profile"]["value"] == "unknown"
    assert caps["secret_store"]["value"] == "unknown"  # ESO store list denied as well


def test_eso_vault_store_counts_as_secret_store():
    answers = {k: RKE2_CIS[k] for k in ("/version", "/apis")}
    answers["/apis/external-secrets.io/v1/clustersecretstores"] = {"items": [
        {"metadata": {"name": "openbao-backend"}, "spec": {"provider": {"vault": {"server": "https://openbao:8200"}}}}]}
    caps, paths = detect_capabilities(fake(answers))
    assert caps["secret_store"] == {"value": "vault-api", "source": "observed: clustersecretstore/openbao-backend provider vault"}
    assert "/apis/external-secrets.io/v1/clustersecretstores" in paths


def test_served_group_list_makes_missing_operator_absent():
    answers = {"/apis": {"kind": "APIGroupList", "groups": [{"name": "apps", "preferredVersion": {"version": "v1"}}]}}
    caps, _ = detect_capabilities(fake(answers))
    assert caps["postgres_operator"]["value"] == "absent"
    # No in-cluster S3 trace does not prove there is no S3-compatible store.
    assert caps["s3_compatible"]["value"] == "unknown"


@pytest.mark.parametrize(("nodes", "value"), [
    ([node(["server"]), node(["agent"])], "absent"),
    ([node(["server", "--profile", "cis"]), node(["agent"])], "unknown"),  # nodes disagree
    ([node(["server", "--profile", "cis"]), node(None)], "unknown"),  # not RKE2 or not exposed
    ([], "unknown"),
])
def test_cis_profile_only_from_consistent_rke2_node_args(nodes, value):
    caps, _ = detect_capabilities(fake({"/api/v1/nodes": {"metadata": {}, "items": nodes}}))
    assert caps["cis_profile"]["value"] == value


def test_truncated_lists_are_unknown():
    answers = {"/api/v1/nodes": {"metadata": {"continue": "x"}, "items": [node(["server", "--profile", "cis"])]},
               "/api/v1/services": {"metadata": {"continue": "x"}, "items": [{"metadata": {"namespace": "a", "name": "minio"}}]}}
    caps, _ = detect_capabilities(fake(answers))
    assert caps["cis_profile"]["value"] == "unknown" and caps["s3_compatible"]["value"] == "unknown"


def test_paths_outside_the_read_list_are_refused_before_the_call():
    calls = []
    reader = _Reader(fake(RKE2_CIS, calls))
    for path in ("/api/v1/namespaces/x/pods", "/api/v1/namespaces/x/secrets", "/apis/postgresql.cnpg.io/v1/clusters"):
        with pytest.raises(ReadRefused):
            reader.read(path)
    assert calls == []


def test_unknown_observation_keeps_the_declared_value_and_absent_blocks_fit():
    profile = {"env_id": "onprem-demo", "env_kind": "onprem", "provides": {"postgres_operator": "cnpg", "secret_store": "openbao"}}
    observed = {"secret_store": {"value": "unknown", "source": "unknown: x"},
                "postgres_operator": {"value": "absent", "source": "observed: /apis"},
                "s3_compatible": {"value": "unknown", "source": "unknown: y"}}
    caps = resolve(profile, observed)
    assert caps["secret_store"] == {"value": "openbao", "source": "declared: profile"}
    assert caps["postgres_operator"]["value"] == "absent" and caps["s3_compatible"]["value"] == "unknown"
    items = [{"role": "db", "grade": "config_swap"}, {"role": "object_store", "grade": "config_swap"}]
    f = fit(items, profile, caps)
    assert f["verdict"] == "blocked"
    assert any("'postgres_operator' 기능이 관측되지 않음" in m for m in f["missing_capabilities"])
    assert any("'s3_compatible' 기능이 등록되지 않음" in m for m in f["missing_capabilities"])


def test_raw_api_exports_feed_the_detector(tmp_path):
    for path, name in (("/version", "version.json"), ("/apis", "apis.json"), ("/api/v1/nodes", "nodes.json")):
        (tmp_path / name).write_text(json.dumps(RKE2_CIS[path]), encoding="utf-8")
    (tmp_path / "statefulsets.json").write_text(json.dumps({"items": [{"metadata": {"name": "target-openbao"}}]}), encoding="utf-8")
    found = observe(tmp_path)
    assert found["k8s_version"]["value"] == "v1.31.4+rke2r1" and found["cis_profile"]["value"] == "rke2-cis"
    # services.json was not exported: the StatefulSet observation stays instead of becoming unknown.
    assert found["secret_store"] == {"value": "openbao", "source": "observed: StatefulSet/target-openbao"}
    assert found["s3_compatible"]["value"] == "unknown"


def test_exports_without_api_answers_keep_the_previous_output(tmp_path):
    (tmp_path / "crds.json").write_text(json.dumps({"items": [{"metadata": {"name": "clusters.postgresql.cnpg.io"}}]}), encoding="utf-8")
    assert observe(tmp_path) == {"postgres_operator": {"value": "cnpg", "source": "observed: CRD/clusters.postgresql.cnpg.io"}}
