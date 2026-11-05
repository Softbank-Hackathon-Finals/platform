"""T31: apply an approved change to the target exactly as approved, or stop."""

from .binding import BindingError, diff_hash, render_diff, verify_claim
from .deploy import DeployMismatch, FakeKube, apply_pinned_image, image_patch
from .path import ApplyOutcome, apply_approved_change

__all__ = ["ApplyOutcome", "BindingError", "DeployMismatch", "FakeKube", "apply_approved_change",
           "apply_pinned_image", "diff_hash", "image_patch", "render_diff", "verify_claim"]
