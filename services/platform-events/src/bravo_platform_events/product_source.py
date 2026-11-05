"""T10 source producer for the existing T15 bucket proposal contract."""

from dataclasses import dataclass, field

from bravo_agent.contracts import SnapshotManifest
from bravo_agent.tools import Snapshot
from rehearsal_runner.bucket_task import BucketPolicyTask
from rehearsal_runner.remote_proposals import bucket_product_bundle


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
