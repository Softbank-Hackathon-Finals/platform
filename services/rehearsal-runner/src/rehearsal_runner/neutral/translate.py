"""Source policy → neutral → target render, the only translation path (ADR-0034 §3).

No pair-specific translator exists: every (source, target) pair is one exporter plus one
renderer, both chosen by kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .apply import OBJECT_RENDERERS, SECRET_RENDERERS, PrincipalFor, Rendered
from .export import OBJECT_EXPORTERS, SECRET_EXPORTERS
from .models import ObjectAccessPolicy, SecretBinding, Workload
from .registry import SOURCE_EXPORT, TARGET_APPLY, Unsupported


@dataclass(frozen=True)
class ObjectTranslation:
    neutral: ObjectAccessPolicy
    rendered: Rendered


def export_object_policy(source_kind: str, text: str, *, bucket: str,
                         principals: Mapping[str, Workload] | None = None) -> ObjectAccessPolicy:
    exporter = OBJECT_EXPORTERS.get(source_kind)
    if exporter is None:
        raise Unsupported(SOURCE_EXPORT, source_kind)
    return exporter(text, bucket=bucket, principals=principals)


def render_object_policy(target_kind: str, policy: ObjectAccessPolicy, *, principal_for: PrincipalFor | None = None,
                         bucket: str | None = None, base: Mapping | None = None) -> Rendered:
    renderer = OBJECT_RENDERERS.get(target_kind)
    if renderer is None:
        raise Unsupported(TARGET_APPLY, target_kind)
    options = {"principal_for": principal_for, "bucket": bucket}
    if target_kind == "onprem":
        options["base"] = base
    return renderer(policy, **options)


def translate_object_policy(source_kind: str, target_kind: str, text: str, *, bucket: str,
                            principals: Mapping[str, Workload] | None = None,
                            principal_for: PrincipalFor | None = None, target_bucket: str | None = None,
                            base: Mapping | None = None) -> ObjectTranslation:
    neutral = export_object_policy(source_kind, text, bucket=bucket, principals=principals)
    return ObjectTranslation(neutral, render_object_policy(target_kind, neutral, principal_for=principal_for,
                                                           bucket=target_bucket, base=base))


def export_secret(source_kind: str, metadata: dict, *, keys, consumers,
                  principals: Mapping[str, Workload] | None = None) -> SecretBinding:
    exporter = SECRET_EXPORTERS.get(source_kind)
    if exporter is None:
        raise Unsupported(SOURCE_EXPORT, source_kind)
    return exporter(metadata, keys=keys, consumers=consumers, principals=principals)


def render_secret(target_kind: str, binding: SecretBinding, **context) -> Rendered:
    renderer = SECRET_RENDERERS.get(target_kind)
    if renderer is None:
        raise Unsupported(TARGET_APPLY, target_kind)
    return renderer(binding, **context)
