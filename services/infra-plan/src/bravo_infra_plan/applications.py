"""Destination Argo CD Applications, one per published path (ADR-0033 3.2 · 3.4).

Each Application points at the exact commit the run published, so the synced revision can be
compared with the run record. Automatic prune stays off: removing a file never deletes a cluster
resource. Self-heal is on for the resources the Application owns.
"""

# The destination Argo CD is T44's install (infra/iac/argocd); T45 only adds its project and Applications.
import json

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


def project(stack, *, repo_url, files):
    """One AppProject per stack: the configuration repository, this cluster, and only what T45 makes.

    The scope is computed from the approved manifests of every publish step (2026-11-07 이상무 review):
    destinations are the namespaces those manifests create or write into, and the cluster-scoped
    allowance is exactly the kinds they contain without a namespace. T44's objects (tailnet routes)
    are never in these files, so this project cannot take them over. The Argo CD server is off, so no
    `default` project exists (kind measurement).
    """
    namespaces, cluster_kinds = set(), set()
    for rendered in files.values():
        for text in rendered.values():
            for item in json.loads(text)["items"]:
                group = item["apiVersion"].rpartition("/")[0]
                namespace = item.get("metadata", {}).get("namespace")
                if item["kind"] == "Namespace":
                    namespaces.add(item["metadata"]["name"])
                if namespace:
                    namespaces.add(namespace)
                else:
                    cluster_kinds.add((group, item["kind"]))
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "AppProject",
        "metadata": {"name": stack, "namespace": ARGO_NAMESPACE,
                     "labels": {"app.kubernetes.io/managed-by": "bravo-infra-plan"}},
        "spec": {
            "sourceRepos": [repo_url],
            "destinations": [{"server": "https://kubernetes.default.svc", "namespace": n} for n in sorted(namespaces)],
            "clusterResourceWhitelist": [{"group": g, "kind": k} for g, k in sorted(cluster_kinds)],
        },
    }


def default_namespace(rendered):
    """The namespace an Application names as its destination: one its own manifests use.

    Argo CD matches `spec.destination.namespace` against the project's destinations with a glob
    (v3.6 AppProject.isDestinationMatched): an empty namespace matches only `*`. The project lists
    exactly the manifests' namespaces, so the Application must name one of them. Every item carries
    its own namespace, so this is only the default and never moves a resource.
    """
    used = set()
    for text in rendered.values():
        for item in json.loads(text)["items"]:
            if item["kind"] == "Namespace":
                used.add(item["metadata"]["name"])
            elif item.get("metadata", {}).get("namespace"):
                used.add(item["metadata"]["namespace"])
    if not used:
        raise ValueError("application_namespace_unavailable")  # cluster-scoped only: no project destination fits
    return sorted(used)[0]


def application(stack, path, *, repo_url, revision, namespace):
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
            "destination": {"server": "https://kubernetes.default.svc", "namespace": namespace},
            "syncPolicy": {
                "automated": {"prune": False, "selfHeal": True},
                "syncOptions": ["ServerSideApply=true", "SkipDryRunOnMissingResource=true",
                                "RespectIgnoreDifferences=true"],
                "retry": {"limit": 5, "backoff": {"duration": "10s", "factor": 2, "maxDuration": "2m"}},
            },
            "ignoreDifferences": IGNORE_DIFFERENCES,
        },
    }
