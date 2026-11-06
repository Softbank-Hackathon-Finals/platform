"""Reading back what a sink actually holds. Used by ``verify``, ``append --resume-from`` and
``follow`` (which resumes from storage, never from a local cursor file)."""

from __future__ import annotations

import json
import re
from pathlib import Path

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class StoreTampered(Exception):
    """The stored run is not a single, untouched version per record."""


def _load_s3_run(client, bucket: str, prefix: str) -> list[dict]:
    """Every version of every record under one run prefix (review #257 P1).

    Reading only the current version would miss a record rewritten without ``If-None-Match``:
    Object Lock keeps the old version but does not stop a new one, so a re-hashed chain would
    verify. A key with more than one version, or a delete marker, is reported as tampering."""
    versions: dict[str, list[str]] = {}
    markers: set[str] = set()
    for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        for v in page.get("Versions", []):
            if v["Key"].endswith(".json"):
                versions.setdefault(v["Key"], []).append(v["VersionId"])
        for m in page.get("DeleteMarkers", []):
            if m["Key"].endswith(".json"):
                markers.add(m["Key"])

    def seq(key: str) -> str:
        name = key.rsplit("/", 1)[-1].removesuffix(".json")
        return str(int(name)) if name.isdigit() else "?"

    for key in sorted(set(versions) | markers):
        if key in markers:
            raise StoreTampered(f"delete_marker:{seq(key)}")
        if len(versions[key]) > 1:
            raise StoreTampered(f"overwritten:{seq(key)}")
    return [json.loads(client.get_object(Bucket=bucket, Key=k, VersionId=versions[k][0])["Body"].read())
            for k in sorted(versions)]


def split_s3(target: str) -> tuple[str, str]:
    """``s3://bucket[/prefix]`` → (bucket, prefix). The prefix defaults to ``audit`` like the sink."""
    bucket, _, prefix = target[5:].partition("/")
    return bucket, prefix.strip("/") or "audit"


def _local_run(path: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(path.glob("*.json"))]


def load_plan_runs(target: str, plan_id: str, client=None) -> dict[str, list[dict]]:
    """Every stored run of one plan under a sink root (``./store`` or ``s3://bucket/audit``).

    Runs are discovered from *all* versions and delete markers under ``<prefix>/<plan>/``, so a
    run whose records were all hidden behind delete markers is still found (and refused). A
    record that sits under another plan/run folder than it names, or an unreadable record, is
    tampering. Nothing is written."""
    if not IDENTIFIER.match(plan_id):
        raise ValueError("invalid plan id")
    runs: dict[str, list[dict]] = {}
    try:
        if target.startswith("s3://"):
            bucket, prefix = split_s3(target)
            plan_prefix = f"{prefix}/{plan_id}/"
            found: set[str] = set()
            for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=plan_prefix):
                for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                    run, sep, _rest = item["Key"][len(plan_prefix):].partition("/")
                    if not sep or not IDENTIFIER.match(run):
                        raise StoreTampered("unexpected_key")
                    found.add(run)
            for run in sorted(found):
                runs[run] = _load_s3_run(client, bucket, f"{plan_prefix}{run}/")
        else:
            plan_dir = Path(target) / "audit" / plan_id
            if plan_dir.is_dir():
                for path in sorted(plan_dir.iterdir()):
                    if not path.is_dir() or not IDENTIFIER.match(path.name):
                        raise StoreTampered("unexpected_key")
                    runs[path.name] = _local_run(path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StoreTampered("unreadable_record") from exc
    for run, records in runs.items():
        for doc in records:
            if not isinstance(doc, dict) or doc.get("plan_id") != plan_id or doc.get("run_id") != run:
                raise StoreTampered("record_outside_its_run")
    return runs
