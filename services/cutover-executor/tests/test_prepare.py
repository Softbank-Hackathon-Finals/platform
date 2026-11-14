from copy import deepcopy

import pytest
from pydantic import ValidationError

from bravo_cutover.prepare import OWNER, OWNER_KEY, cluster_patch, object_names, pooler_trust_patch
from bravo_cutover.sites import Site


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


def named_site(**names) -> Site:
    """A gcp source whose CNPG objects are not named by the {side}-db rule (ADR-0034, A9)."""
    return Site(environment_id="gcp-a", kind="gcp", resource_id="cnpg-gcp-a/chat", connection_ref="ref/gcp-a",
                kube_context="ctx", namespace="chat", subscription="bravo_rev", db_host="db.chat",
                pgbouncer_admin_dsn="x", pgbouncer_app_dsn="x", db_control_dsn="x", db_app_dsn="x", **names)


def test_object_names_default_to_side_rule_when_site_leaves_them_empty():
    assert object_names("source") == object_names("source", named_site())
    names = object_names("target", named_site())
    assert (names.cluster, names.namespace, names.pooler) == ("target-db", "target-dev", "target-pgbouncer")
    with pytest.raises(ValueError):
        object_names("origin")


def test_cluster_and_pooler_names_are_read_from_site():
    site = named_site(db_cluster="chat-pg", db_namespace="chat-data", pgbouncer_deployment="chat-pooler")
    current = cluster()
    current["metadata"].update(name="chat-pg", namespace="chat-data")
    assert cluster_patch(current, side="source", site=site)[0]["value"] == "41"
    with pytest.raises(ValueError, match="unexpected database resource"):
        cluster_patch(current, side="source")  # the default rule no longer matches
    with pytest.raises(ValueError, match="unexpected database resource"):
        cluster_patch(cluster(), side="source", site=site)  # nor does the default name under a configured site

    pooler = {"kind": "Deployment", "metadata": {"name": "chat-pooler", "namespace": "chat-data", "resourceVersion": "7"},
              "spec": {"replicas": 1, "template": {"spec": {"volumes": [
                  {"name": "ca", "secret": {"secretName": "cutover-backend-ca"}}]}}}}
    assert pooler_trust_patch(pooler, side="target", site=site) == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "7"}]
    with pytest.raises(ValueError, match="unexpected singleton pooler"):
        pooler_trust_patch(pooler, side="target")


@pytest.mark.parametrize("field", ["db_cluster", "db_namespace", "pgbouncer_deployment"])
def test_configured_object_names_must_be_kubernetes_names(field):
    with pytest.raises(ValidationError):
        named_site(**{field: "Bad_Name"})
