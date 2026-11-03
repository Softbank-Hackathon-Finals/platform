import copy

import pytest

from bravo_controls.backup_rbac import backup_readback_rbac

ARGS = dict(
    namespace="backup-test",
    identity="reader-attempt-1",
    source_cluster="chat-db",
    object_store="archive",
    backup="backup-attempt-1",
)


def test_exact_resources_and_no_implicit_credentials_or_writes():
    account, role, binding = backup_readback_rbac(**ARGS)
    assert account["automountServiceAccountToken"] is False
    assert "secrets" not in account
    assert role["kind"] == "Role"
    assert role["rules"] == [
        {
            "apiGroups": ["postgresql.cnpg.io"],
            "resources": ["clusters"],
            "resourceNames": ["chat-db"],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["postgresql.cnpg.io"],
            "resources": ["backups"],
            "resourceNames": ["backup-attempt-1"],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["barmancloud.cnpg.io"],
            "resources": ["objectstores"],
            "resourceNames": ["archive"],
            "verbs": ["get"],
        },
    ]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": ARGS["identity"],
    }
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": ARGS["identity"], "namespace": ARGS["namespace"]}
    ]
    assert all(
        obj["metadata"]["namespace"] == ARGS["namespace"] for obj in (account, role, binding)
    )


@pytest.mark.parametrize("field", ARGS)
@pytest.mark.parametrize("bad", ["*", "", "../secret", "x/y", "UPPER", "a" * 64, None])
def test_wildcard_or_invalid_registered_names_rejected(field, bad):
    with pytest.raises(ValueError):
        backup_readback_rbac(**(ARGS | {field: bad}))


def test_manifests_do_not_share_mutable_metadata():
    manifests = backup_readback_rbac(**ARGS)
    retained = copy.deepcopy(manifests)
    manifests[0]["metadata"]["labels"]["component"] = "other"
    assert manifests[1:] == retained[1:]
    assert backup_readback_rbac(**ARGS) == retained
