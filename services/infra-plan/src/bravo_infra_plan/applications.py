"""Destination Argo CD Applications, one per published path (ADR-0033 3.2 · 3.4).

Each Application points at the exact commit the run published, so the synced revision can be
compared with the run record. Automatic prune stays off: removing a file never deletes a cluster
resource. Self-heal is on for the resources the Application owns.
"""

# The destination Argo CD is T44's install (infra/iac/argocd); T45 only adds its project and Applications.
ARGO_NAMESPACE = "bravo-iac-gitops"


def application_name(stack, path):
    name = f"{stack}-{path}"
    if len(name) > 63:  # truncating could make two paths share one Application
        raise ValueError("application_name_too_long")
    return name


# Fields that controllers fill in after apply; Git never carries them. Empty or default values
# are omitted: the API server drops them, and keeping them makes every server-side diff non-empty.
IGNORE_DIFFERENCES = [
    {"kind": "Secret", "name": "external-secrets-webhook", "jsonPointers": ["/data"]},
    {"group": "apiextensions.k8s.io", "kind": "CustomResourceDefinition",
     "jqPathExpressions": [".spec.conversion.webhook.clientConfig.caBundle"]},
    {"group": "admissionregistration.k8s.io", "kind": "ValidatingWebhookConfiguration",
     "jqPathExpressions": [".webhooks[]?.clientConfig.caBundle"]},
]


def project(stack, *, repo_url):
    """One AppProject per stack, limited to the configuration repository and this cluster.

    The Argo CD server is off, so no `default` project exists (found in the kind measurement), and
    T44's own project `bravo-iac` is limited to the public platform repository.
    Cluster-scoped kinds stay open in v1 (Namespaces, PersistentVolumes, operator CRDs and
    ClusterRoles); narrowing them is the agreed follow-up (ADR-0033 3.5).
    """
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "AppProject",
        "metadata": {"name": stack, "namespace": ARGO_NAMESPACE,
                     "labels": {"app.kubernetes.io/managed-by": "bravo-infra-plan"}},
        "spec": {
            "sourceRepos": [repo_url],
            "destinations": [{"server": "https://kubernetes.default.svc", "namespace": "*"}],
            "clusterResourceWhitelist": [{"group": "*", "kind": "*"}],
        },
    }


def application(stack, path, *, repo_url, revision):
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": application_name(stack, path), "namespace": ARGO_NAMESPACE,
                     "labels": {"app.kubernetes.io/managed-by": "bravo-infra-plan",
                                "bravo.infra/stack": stack}},
        "spec": {
            "project": stack,
            "source": {"repoURL": repo_url, "targetRevision": revision, "path": path,
                       "directory": {"include": "*.json"}},
            "destination": {"server": "https://kubernetes.default.svc"},
            "syncPolicy": {
                "automated": {"prune": False, "selfHeal": True},
                "syncOptions": ["ServerSideApply=true", "SkipDryRunOnMissingResource=true",
                                "RespectIgnoreDifferences=true"],
                "retry": {"limit": 5, "backoff": {"duration": "10s", "factor": 2, "maxDuration": "2m"}},
            },
            "ignoreDifferences": IGNORE_DIFFERENCES,
        },
    }
