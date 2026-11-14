"""Credential rehearsal by target kind (ADR-0034 `target.rehearsal`).

* onprem: the existing OpenBao flow (`credential_job.CredentialJob`), unchanged. Its constants
  are grouped here as ``OPENBAO`` so the target kind selects them.
* aws / gcp: the cloud secret store + ESO. This module builds the manifests a rehearsal applies
  (writer Job in the control namespace, reader ServiceAccount + SecretStore + ExternalSecret in
  the rehearsal namespace, cleanup Job) from the neutral ``SecretBinding`` render. They are
  tested as specs only: nothing here has run on EKS or GKE, so ``runnable`` is False and the
  `target.rehearsal` cell stays ``UNSUPPORTED`` until A11 verifies them.

A synthetic value is written by the writer Job only, from a Secret in the control namespace; it is
never logged, rendered into a manifest here, or returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import credential_job as openbao
from .credential_job import _container_security, _pod_security
from .neutral.apply import Rendered
from .neutral.models import SecretBinding
from .neutral.registry import UNSUPPORTED
from .neutral.translate import render_secret


@dataclass(frozen=True)
class OpenBaoCredentialTarget:
    kind: str = "onprem"
    runnable: bool = True
    control_namespace: str = openbao.CONTROL_NAMESPACE
    writer_account: str = openbao.WRITER_ACCOUNT
    reader_account: str = openbao.READER_ACCOUNT
    intruder_account: str = openbao.INTRUDER_ACCOUNT
    store_address: str = openbao.OPENBAO_ADDR
    ca_configmap: str = openbao.CA_CONFIGMAP

    def job_class(self) -> type:
        return openbao.CredentialJob


OPENBAO = OpenBaoCredentialTarget()

WRITER_SCRIPTS = {
    # Each prints one JSON line and exits 0; the runner judges the observation.
    "aws": ('set -u; value=failed; '
            'aws secretsmanager create-secret --name "$REMOTE" --secret-string file:///work/synthetic.json '
            '--tags Key=component,Value=rehearsal >/dev/null 2>&1 && value=ok; '
            'printf \'{"value":"%s"}\\n\' "$value"'),
    "gcp": ('set -u; value=failed; '
            'gcloud secrets create "$REMOTE" --project "$PROJECT" --replication-policy automatic '
            '--labels component=rehearsal --data-file=/work/synthetic.json >/dev/null 2>&1 && value=ok; '
            'printf \'{"value":"%s"}\\n\' "$value"'),
}
CLEANUP_SCRIPTS = {
    "aws": ('set -u; value=failed; '
            'aws secretsmanager delete-secret --secret-id "$REMOTE" --force-delete-without-recovery '
            '>/dev/null 2>&1 && value=deleted; printf \'{"value":"%s"}\\n\' "$value"'),
    "gcp": ('set -u; value=failed; '
            'gcloud secrets delete "$REMOTE" --project "$PROJECT" --quiet >/dev/null 2>&1 && value=deleted; '
            'printf \'{"value":"%s"}\\n\' "$value"'),
}
_REHEARSAL = re.compile(r"rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?")
_IMAGE = re.compile(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}")


@dataclass(frozen=True)
class EsoCredentialTarget:
    """aws: Secrets Manager + IRSA. gcp: Secret Manager + GKE Workload Identity."""

    kind: str
    runnable: bool = False
    control_namespace: str = openbao.CONTROL_NAMESPACE
    writer_account: str = "rehearsal-secret-writer"
    reader_account: str = "rehearsal-eso-reader"
    store: str = "rehearsal-secrets"

    def remote_key(self, namespace: str, binding: SecretBinding) -> str:
        """One remote secret per rehearsal namespace, so cleanup never touches another attempt."""
        if _REHEARSAL.fullmatch(namespace) is None:
            raise ValueError("rehearsal namespace required")
        sep = "/" if self.kind == "aws" else "-"
        return sep.join(("bravo-rehearsal", namespace, binding.logical_name))

    def render(self, binding: SecretBinding, namespace: str, **context) -> Rendered:
        """The neutral render (`target.neutral_apply`) for this rehearsal namespace's consumer."""
        if not any(c.namespace == namespace for c in binding.consumers):
            # The rehearsal runs the app as the binding's workload, moved into the rehearsal namespace.
            raise ValueError("binding has no consumer in the rehearsal namespace")
        return render_secret(self.kind, binding, namespace=namespace, remote_key=self.remote_key(namespace, binding),
                             store_service_account=self.reader_account, store=self.store, **context)

    def rehearsal_manifests(self, binding: SecretBinding, namespace: str, meta: dict, *,
                            reader_annotations: dict[str, str] | None = None, **context) -> list[dict]:
        """Reader ServiceAccount, SecretStore and ExternalSecret in the rehearsal namespace."""
        rendered = self.render(binding, namespace, **context)
        account = {"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
                   "metadata": meta | {"name": self.reader_account,
                                       "annotations": {**meta.get("annotations", {}), **(reader_annotations or {})}}}
        out = [account]
        for manifest in rendered.document:
            out.append({**manifest, "metadata": {**manifest["metadata"], "labels": meta.get("labels", {}),
                                                  "annotations": meta.get("annotations", {})}})
        return out

    def writer_job(self, binding: SecretBinding, namespace: str, meta: dict, *, image: str, work_secret: str,
                   project_id: str = "", region: str = "", deadline_seconds: int = 180) -> dict:
        return self._job(WRITER_SCRIPTS[self.kind], f"t15-cred-{namespace}-write", binding, namespace, meta,
                         image=image, work_secret=work_secret, project_id=project_id, region=region,
                         deadline_seconds=deadline_seconds)

    def cleanup_job(self, binding: SecretBinding, namespace: str, meta: dict, *, image: str,
                    project_id: str = "", region: str = "", deadline_seconds: int = 180) -> dict:
        return self._job(CLEANUP_SCRIPTS[self.kind], f"t15-cred-{namespace}-cleanup", binding, namespace, meta,
                         image=image, work_secret=None, project_id=project_id, region=region,
                         deadline_seconds=deadline_seconds)

    def _job(self, script: str, name: str, binding: SecretBinding, namespace: str, meta: dict, *, image: str,
             work_secret: str | None, project_id: str, region: str, deadline_seconds: int) -> dict:
        if _IMAGE.fullmatch(image or "") is None:
            raise ValueError("image must be pinned by digest")
        env = [{"name": "REMOTE", "value": self.remote_key(namespace, binding)}, {"name": "HOME", "value": "/tmp"}]
        if self.kind == "aws":
            if not re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-[0-9]", region):
                raise ValueError("aws region required")
            env += [{"name": "AWS_DEFAULT_REGION", "value": region}, {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
                    {"name": "AWS_MAX_ATTEMPTS", "value": "1"}]
        else:
            if not re.fullmatch(r"[a-z][-a-z0-9]{4,28}[a-z0-9]", project_id):
                raise ValueError("gcp project required")
            env += [{"name": "PROJECT", "value": project_id}, {"name": "CLOUDSDK_CONFIG", "value": "/tmp/gcloud"},
                    {"name": "CLOUDSDK_CORE_DISABLE_PROMPTS", "value": "1"}]
        mounts, volumes = [{"name": "tmp", "mountPath": "/tmp"}], [{"name": "tmp", "emptyDir": {}}]
        if work_secret:
            mounts.append({"name": "work", "mountPath": "/work", "readOnly": True})
            volumes.append({"name": "work", "secret": {"secretName": work_secret}})
        container = {"name": "run", "image": image, "imagePullPolicy": "IfNotPresent",
                     "command": ["/bin/sh", "-c", script], "env": env, "volumeMounts": mounts,
                     "resources": {"requests": {"cpu": "20m", "memory": "64Mi"},
                                   "limits": {"cpu": "500m", "memory": "256Mi"}},
                     "securityContext": _container_security()}
        return {"apiVersion": "batch/v1", "kind": "Job",
                "metadata": {**meta, "namespace": self.control_namespace, "name": name},
                "spec": {"backoffLimit": 0, "activeDeadlineSeconds": deadline_seconds, "template": {
                    "metadata": {"labels": {"role": "credential-verifier",
                                            "app.kubernetes.io/component": "rehearsal-secret-writer"}},
                    "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                             "serviceAccountName": self.writer_account, "securityContext": _pod_security(65532),
                             "volumes": volumes, "containers": [container]}}}}


CREDENTIAL_TARGETS = {"onprem": OPENBAO, "aws": EsoCredentialTarget("aws"), "gcp": EsoCredentialTarget("gcp")}


def credential_target(kind: str):
    """The credential rehearsal for a target kind, or ``UNSUPPORTED``."""
    return CREDENTIAL_TARGETS.get(kind, UNSUPPORTED)
