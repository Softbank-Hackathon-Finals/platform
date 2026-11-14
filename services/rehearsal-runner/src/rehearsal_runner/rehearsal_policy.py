"""Bucket rehearsal translation: the task's source policy → neutral → the target's rehearsal dialect.

This replaces the pair-specific "S3 policy → SeaweedFS" assumption. The rehearsal applies policies
through the target's own bucket-policy API: S3 bucket policy on aws, the S3 bucket-policy API of
SeaweedFS on onprem, and bucket IAM on gcp. The deterministic render is the control the model's
proposal is compared against (`bucket_job.SeaweedBucketJob.control`).

aws → onprem keeps applying the source policy as is: SeaweedFS accepts the S3 policy, and the
golden test shows the render has the same meaning (`tests/test_neutral_golden.py`).
"""

from __future__ import annotations

from collections.abc import Mapping

from .bucket_task import BucketPolicyTask
from .neutral.apply import PrincipalFor, render_gcs_iam_policy, render_s3_bucket_policy
from .neutral.models import Workload
from .neutral.registry import TARGET_REHEARSAL, Unsupported
from .neutral.translate import ObjectTranslation, export_object_policy

REHEARSAL_RENDERERS = {"onprem": render_s3_bucket_policy, "aws": render_s3_bucket_policy,
                       "gcp": render_gcs_iam_policy}


def task_translation(task: BucketPolicyTask, *, principals: Mapping[str, Workload] | None = None,
                     principal_for: PrincipalFor | None = None) -> ObjectTranslation:
    renderer = REHEARSAL_RENDERERS.get(task.target_kind)
    if renderer is None:
        raise Unsupported(TARGET_REHEARSAL, task.target_kind)
    neutral = export_object_policy(task.source_kind, task.source_policy, principals=principals,
                                   bucket=task.target_bucket if task.source_kind == "gcp" else None)
    return ObjectTranslation(neutral, renderer(neutral, principal_for=principal_for, bucket=task.target_bucket))


def control_policy(task: BucketPolicyTask, **options):
    """``SeaweedBucketJob.control`` for ``task``: ``None`` keeps the source as is (aws → onprem)."""
    if (task.source_kind, task.target_kind) == ("aws", "onprem"):
        return None
    text = task_translation(task, **options).rendered.text()

    def control(source: str) -> str:
        if source != task.source_policy:
            raise ValueError("source policy changed")
        return text

    return control
