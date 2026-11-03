from copy import deepcopy

import pytest

from bravo_cutover.prepare import OWNER, OWNER_KEY, cluster_patch, pooler_trust_patch


def cluster():
    return {"kind": "Cluster", "metadata": {"name": "source-db", "namespace": "source-dev", "resourceVersion": "41"},
            "spec": {"managed": {"roles": [{"name": "chat_app", "login": True},
                                           {"name": "chat_replicator", "replication": True}]},
                     "postgresql": {"pg_hba": ["hostnossl all all all reject", "host all all all reject"]}}}


def test_handoff_does_not_drop_role_or_edit_existing_replication_role():
    original = cluster()
    before = deepcopy(original)
    patches = cluster_patch(original, side="source")
    assert original == before
    assert patches[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "41"}
    roles = next(p["value"] for p in patches if p["path"] == "/spec/managed/roles")
    assert roles[0] == original["spec"]["managed"]["roles"][1]
    assert [r["name"] for r in roles] == ["chat_replicator", "cutover_control"]
    assert "absent" not in str(patches)
    assert roles[-1]["createrole"] is False
    original["metadata"]["annotations"] = {OWNER_KEY: OWNER}
    original["spec"]["managed"]["roles"] = roles
    original["spec"]["postgresql"]["pg_hba"] = next(p["value"] for p in patches if p["path"].endswith("pg_hba"))
    assert len(cluster_patch(original, side="source")) == 1  # idempotent read-version assertion only


@pytest.mark.parametrize("change", ["namespace", "owner", "missing_app", "different_control"])
def test_unconfirmed_ownership_is_rejected(change):
    current = cluster()
    if change == "namespace":
        current["metadata"]["namespace"] = "other"
    elif change == "owner":
        current["metadata"]["annotations"] = {OWNER_KEY: "someone-else"}
    elif change == "missing_app":
        current["spec"]["managed"]["roles"].pop(0)
    else:
        current["spec"]["managed"]["roles"].append({"name": "cutover_control", "superuser": True})
    with pytest.raises(ValueError):
        cluster_patch(current, side="source")


def test_pooler_changes_only_backend_trust_mount():
    current = {"kind": "Deployment", "metadata": {"name": "target-pgbouncer", "namespace": "target-dev", "resourceVersion": "7"},
               "spec": {"replicas": 1, "template": {"spec": {"volumes": [
                   {"name": "ca", "secret": {"secretName": "original-ca"}},
                   {"name": "tls", "secret": {"secretName": "server-tls"}}]}}}}
    before = deepcopy(current)
    patches = pooler_trust_patch(current, side="target")
    assert current == before
    assert patches[-1]["path"] == "/spec/template/spec/volumes/0/secret/secretName"
    assert patches[-1]["value"] == "cutover-backend-ca"
    assert len(patches) == 2
