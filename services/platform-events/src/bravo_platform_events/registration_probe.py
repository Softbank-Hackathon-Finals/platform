"""Read-only registration adapter for the P0 EKS and on-prem Kubernetes pair."""

import hashlib
import json
import os
import re
import ssl
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class ProbeUnavailable(RuntimeError):
    """Safe public error. Raw kube, TLS and git diagnostics stay private."""


@dataclass(frozen=True)
class EnvironmentRef:
    tenant_id: str
    environment_id: str
    kind: str
    namespace: str
    database: str
    database_uid: str
    endpoint: str
    ca_file: Path
    read_token: Callable[[], str] = field(repr=False)
    tls_name: str | None = None

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("invalid_registration_endpoint")
        if self.kind not in ("aws", "onprem") or not self.database_uid:
            raise ValueError("invalid_environment_reference")
        for name in (self.namespace, self.database):
            if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name):
                raise ValueError("invalid_resource_name")

    @property
    def role(self):
        return {"aws": "source", "onprem": "target"}[self.kind]


@dataclass(frozen=True)
class SourceRelease:
    """Host-approved repository checkout and build provenance for one workload."""
    tenant_id: str
    environment_id: str
    repo_url: str
    checkout: Path
    app_path: str
    iac_path: str
    revision: str
    label: str
    container: str
    image: str  # Full, immutable repository@sha256 reference in the deployment.
    runtime_digests: tuple[str, ...]  # Authorized manifests/config from that build.
    service_account: str
    replicaset_uid: str  # From the rollout owner's Deployment -> ReplicaSet readback.

    def __post_init__(self):
        digest = r"sha256:[0-9a-f]{64}"
        if (not re.fullmatch(r"[0-9a-f]{40}", self.revision)
                or not re.fullmatch(r"[^\s@]+@" + digest, self.image)
                or not self.runtime_digests or any(not re.fullmatch(digest, d) for d in self.runtime_digests)):
            raise ValueError("invalid_release_identity")
        if not self.replicaset_uid:
            raise ValueError("missing_rollout_identity")
        for path in (self.app_path, self.iac_path):
            if path != "." and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*", path):
                raise ValueError("invalid_release_path")

    def verify_checkout(self):
        def git(*args):
            return subprocess.run(["git", "-C", self.checkout, *args], check=True, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=5, text=True,
                                  env={"PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_GLOBAL": os.devnull,
                                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
                                                    "GIT_TERMINAL_PROMPT": "0"}).stdout.strip()
        try:
            if git("remote", "get-url", "origin").removesuffix(".git") != self.repo_url.removesuffix(".git"):
                raise ProbeUnavailable("repo_mismatch")
            if git("rev-parse", "--verify", self.revision + "^{commit}") != self.revision:
                raise ProbeUnavailable("revision_mismatch")
            for path, types in [(self.app_path, ("tree",)), (self.iac_path, ("tree", "blob"))]:
                obj = self.revision + ":" + ("" if path == "." else path)
                if git("cat-file", "-t", obj) not in types:
                    raise ProbeUnavailable("repo_path_unavailable")
        except (OSError, subprocess.SubprocessError):
            raise ProbeUnavailable("repo_unavailable") from None


def _collector_access(review, env):
    subject = "system:serviceaccount:" + env.namespace + ":bravo-collector"
    if review("authentication.k8s.io", "SelfSubjectReview")["userInfo"]["username"] != subject:
        raise ProbeUnavailable("collector_identity_mismatch")
    auth = "authorization.k8s.io"
    reads = [("postgresql.cnpg.io", "clusters", "get", env.database),
             ("", "pods", "list", ""), ("", "services", "list", "")]
    def decision(group, resource, verb, name=""):
        attrs = dict(namespace=env.namespace, group=group, resource=resource, verb=verb)
        if name:
            attrs["name"] = name
        value = review(auth, "SelfSubjectAccessReview", {"resourceAttributes": attrs})
        if value.get("evaluationError") or type(value.get("allowed")) is not bool:
            return "unknown"
        if value["allowed"] and value.get("denied"):
            return "unknown"
        return "allowed" if value["allowed"] else "denied"

    if any(decision(*action) != "allowed" for action in reads):
        raise ProbeUnavailable("required_read_denied")
    writes = {"create_pods": decision("", "pods", "create"),
              "patch_registered_cnpg": decision("postgresql.cnpg.io", "clusters", "patch", env.database)}
    rules = review(auth, "SelfSubjectRulesReview", {"namespace": env.namespace})
    warnings = []
    if rules.get("incomplete") is not False or rules.get("evaluationError"):
        warnings.append("write_scope_unknown")
    for rule in rules["resourceRules"]:
        # Ignore Kubernetes' default ability to create self-review requests.
        mutates = set(rule["verbs"]) - {"get", "list", "watch"}
        review_only = set(rule.get("apiGroups", [])) <= {"authentication.k8s.io", "authorization.k8s.io"} and set(rule["resources"]) <= {
            "selfsubjectaccessreviews", "selfsubjectrulesreviews", "selfsubjectreviews"}
        if mutates and not review_only:
            warnings.append("namespace_write_granted")
            break
    if "allowed" in writes.values() and "namespace_write_granted" not in warnings:
        warnings.append("namespace_write_granted")
    if "unknown" in writes.values() and "write_scope_unknown" not in warnings:
        warnings.append("write_scope_unknown")
    return subject, warnings, writes


def _pod_references(pod, container, env, release):
    found = {("ServiceAccount", release.service_account), ("Cluster", env.database)}
    for item in container.get("env", []):
        source = item.get("valueFrom", {})
        for key, kind in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
            if key in source:
                found.add((kind, source[key]["name"]))
    for item in container.get("envFrom", []):
        for key, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
            if key in item:
                found.add((kind, item[key]["name"]))
    mounts = {m["name"] for m in container.get("volumeMounts", [])}
    for volume in pod["spec"].get("volumes", []):
        if volume["name"] not in mounts:
            continue
        if "secret" in volume:
            found.add(("Secret", volume["secret"]["secretName"]))
        if "configMap" in volume:
            found.add(("ConfigMap", volume["configMap"]["name"]))
    return found


def _observe_release(env, release, request):
    if (release.tenant_id, release.environment_id, env.role) != (env.tenant_id, env.environment_id, "source"):
        raise ProbeUnavailable("release_source_mismatch")
    release.verify_checkout()
    pods = request("GET", f"/api/v1/namespaces/{env.namespace}/pods",
                   params={"labelSelector": "app=" + release.label, "limit": "100"})
    if pods["metadata"].get("continue"):
        raise ProbeUnavailable("incomplete_pod_read")
    active = [p for p in pods["items"] if not p["metadata"].get("deletionTimestamp")]
    if not active:
        raise ProbeUnavailable("workload_unavailable")
    refs = None
    for pod in active:
        containers = [i for i in pod["spec"]["containers"] if i["name"] == release.container]
        statuses = [i for i in pod["status"]["containerStatuses"] if i["name"] == release.container]
        owners = [o for o in pod["metadata"].get("ownerReferences", []) if o.get("controller") is True]
        if (len(owners) != 1 or owners[0].get("apiVersion") != "apps/v1" or owners[0].get("kind") != "ReplicaSet"
                or owners[0].get("uid") != release.replicaset_uid
                or pod["metadata"]["namespace"] != env.namespace
                or pod["metadata"]["labels"].get("app") != release.label
                or pod["spec"]["serviceAccountName"] != release.service_account
                or len(containers) != 1 or containers[0]["image"] != release.image
                or len(statuses) != 1 or statuses[0].get("ready") is not True
                or statuses[0]["imageID"].split("@")[-1] not in release.runtime_digests):
            raise ProbeUnavailable("deployment_release_mismatch")
        found = _pod_references(pod, containers[0], env, release)
        if refs is not None and refs != found:
            raise ProbeUnavailable("replica_scope_mismatch")
        refs = found
    return dict(resources=[dict(kind=k, name=n) for k, n in sorted(refs)],
                rollout_identity=hashlib.sha256(release.replicaset_uid.encode()).hexdigest(),
                source_sha=release.revision, image=release.image,
                service_account=release.service_account, repo_deploy_match=True)


class KubernetesProbe:
    """Use a fixed TLS endpoint/CA and one scoped, rotatable collector token."""

    def inspect(self, env: EnvironmentRef, release: SourceRelease | None = None):
        try:
            trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            trust.minimum_version = ssl.TLSVersion.TLSv1_2
            trust.load_verify_locations(cafile=str(env.ca_file))
            token = env.read_token().strip()
            if not token or not token.isascii() or any(c.isspace() for c in token):
                raise ProbeUnavailable("credential_unavailable")
            with httpx.Client(base_url=env.endpoint, verify=trust, trust_env=False, follow_redirects=False,
                              headers={"Authorization": "Bearer " + token}, timeout=5) as client:
                def request(method, path, *, body=None, params=None):
                    ext = {"sni_hostname": env.tls_name} if env.tls_name else {}
                    with client.stream(method, path, json=body, params=params, extensions=ext) as response:
                        if response.status_code in (401, 403):
                            raise ProbeUnavailable("collector_denied")
                        response.raise_for_status()
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            if len(data) + len(chunk) > 2 * 1024 * 1024:
                                raise ProbeUnavailable("probe_response_too_large")
                            data.extend(chunk)
                        return json.loads(data)

                def review(group, kind, spec=None):
                    obj = {"apiVersion": group + "/v1", "kind": kind}
                    if spec is not None:
                        obj["spec"] = spec
                    return request("POST", f"/apis/{group}/v1/{kind.lower()}s", body=obj)["status"]

                subject, warnings, writes = _collector_access(review, env)
                cluster = request("GET", f"/apis/postgresql.cnpg.io/v1/namespaces/{env.namespace}/clusters/{env.database}")
                meta = cluster["metadata"]
                if (cluster["kind"] != "Cluster" or meta["name"] != env.database or meta["namespace"] != env.namespace
                        or meta["uid"] != env.database_uid or meta.get("deletionTimestamp")):
                    raise ProbeUnavailable("environment_replaced")
                observed = dict(namespace=env.namespace, database=env.database,
                                database_identity=hashlib.sha256(env.database_uid.encode()).hexdigest(),
                                collector=subject, read_access="verified", warnings=warnings, write_checks=writes)
                if release is not None:
                    observed.update(_observe_release(env, release, request))
                return observed
        except ProbeUnavailable:
            raise
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ProbeUnavailable("registration_probe_unavailable") from None
