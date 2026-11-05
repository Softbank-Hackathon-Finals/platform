"""T10 source producers for existing T15 task contracts."""

from dataclasses import dataclass, field

from bravo_agent.contracts import FailureBundle, SnapshotManifest, digest
from bravo_agent.tools import Snapshot
from rehearsal_runner.bucket_task import BucketPolicyTask
from rehearsal_runner.credential_proposer import SOURCE_PATHS, credential_summary
from rehearsal_runner.remote_proposals import bucket_product_bundle
from rehearsal_runner.secret_task import CredentialMigrationTask


@dataclass(frozen=True)
class BucketSnapshotSource:
    """Use a host-selected approved snapshot, never a caller-supplied path.

    The host must authenticate the snapshot manifest and its source revision
    before construction. Snapshot.read owns path/hash/secret validation. Only
    the two policy files cross this port; no model, network or filesystem write.
    """

    snapshot: Snapshot = field(repr=False)
    source_policy_path: str
    target_policy_path: str

    def __call__(self, connection, plan, admission, task):
        if (type(task) is not BucketPolicyTask or not isinstance(self.snapshot, Snapshot)
                or task.source_sha != self.snapshot.manifest.source_sha
                or task.run_id != admission.rehearsal_id
                or self.source_policy_path == self.target_policy_path
                or self.target_policy_path not in self.snapshot.manifest.editable_paths):
            raise ValueError("approved bucket snapshot required")
        files = {name: self.snapshot.read(name)
                 for name in (self.source_policy_path, self.target_policy_path)}
        if files[self.source_policy_path] != task.source_policy:
            raise ValueError("snapshot differs from registered catalog source")
        manifest = SnapshotManifest(source_sha=task.source_sha,
            files={name: self.snapshot.manifest.files[name] for name in files},
            editable_paths=[self.target_policy_path])
        return dict(bundle=bucket_product_bundle(task, self.source_policy_path, self.target_policy_path),
                    manifest=manifest, files=files)


@dataclass(frozen=True)
class CredentialSnapshotSource:
    """Read the three T15 credential source files from a host-approved snapshot.

    Admission/current plan/catalog authorization remains ProductInputRecords'
    responsibility. The host authenticates this manifest and immutable checkout
    before construction. This port creates neither a grant nor an Admission.
    """

    snapshot: Snapshot = field(repr=False)

    def __call__(self, connection, plan, admission, task):
        if (type(task) is not CredentialMigrationTask or not isinstance(self.snapshot, Snapshot)
                or task.source_sha != self.snapshot.manifest.source_sha
                or task.source_sha != admission.source_sha or task.run_id != admission.rehearsal_id
                or "app/db_secret.py" not in self.snapshot.manifest.editable_paths):
            raise ValueError("approved credential snapshot required")
        # Snapshot.read verifies the current bytes and path/credential policy.
        files = {name: self.snapshot.read(name) for name in SOURCE_PATHS}
        manifest = SnapshotManifest(source_sha=task.source_sha,
            files={name: self.snapshot.manifest.files[name] for name in SOURCE_PATHS},
            editable_paths=[name for name in SOURCE_PATHS if name in self.snapshot.manifest.editable_paths])
        bundle = FailureBundle(run_id=task.run_id, source_sha=task.source_sha,
            failure_code="CREDENTIAL_MIGRATION", summary=credential_summary(task),
            evidence_digest=digest(manifest))
        return dict(bundle=bundle, manifest=manifest, files=files)
