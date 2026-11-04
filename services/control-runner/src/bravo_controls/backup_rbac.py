"""Render per-attempt CNPG status-reader RBAC; never apply or issue a token.

The environment owner validates tenant, namespace, resource UIDs and approval
before create. Kubernetes RBAC names do not pin UIDs: CNPGBackupReader performs
that independent readback check. This identity is not a backup/restore executor.
"""

from .cnpg_backup import _names


def backup_readback_rbac(*, namespace, identity, source_cluster, object_store, backup):
    """Only GET on three named objects. No Secrets, list/watch, exec or writes.

    Use a fresh identity for each attempt; do not merge these rules into an app,
    executor, Collector or broad role. TokenRequest belongs to the authorized
    dispatcher, with the API server's intended audience and bounded lifetime.
    """
    _names(namespace, identity, source_cluster, object_store, backup)
    return _named_get_rbac(
        namespace,
        identity,
        (
            ("postgresql.cnpg.io", "clusters", source_cluster),
            ("postgresql.cnpg.io", "backups", backup),
            ("barmancloud.cnpg.io", "objectstores", object_store),
        ),
    )


def restore_readback_rbac(*, namespace, identity, restored_cluster, reader_store):
    """GET only the new restore and read-only archive reference, not source/Backup.

    SQL authentication and the registered connection resolver are separate from
    this Kubernetes identity. No Secret or Pod exec/read permission is added.
    """
    _names(namespace, identity, restored_cluster, reader_store)
    return _named_get_rbac(
        namespace,
        identity,
        (
            ("postgresql.cnpg.io", "clusters", restored_cluster),
            ("barmancloud.cnpg.io", "objectstores", reader_store),
        ),
    )


def _named_get_rbac(namespace, identity, resources):
    labels = {"app.kubernetes.io/part-of": "bravo", "component": "backup"}

    def metadata():
        return {"namespace": namespace, "name": identity, "labels": dict(labels)}

    return (
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": metadata(),
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": metadata(),
            "rules": [
                {
                    "apiGroups": [group],
                    "resources": [resource],
                    "resourceNames": [name],
                    "verbs": ["get"],
                }
                for group, resource, name in resources
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": metadata(),
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": identity},
            "subjects": [{"kind": "ServiceAccount", "name": identity, "namespace": namespace}],
        },
    )
