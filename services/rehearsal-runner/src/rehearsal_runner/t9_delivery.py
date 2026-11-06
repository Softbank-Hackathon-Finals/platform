"""Adapter from T9's renderer (`infra/envs/target-secrets/render.py`) to `OpenBaoDelivery`.

T15 does not copy T9's paths, role body or init container; the host loads the renderer the
target was installed with. The renderer is a script, not a package, so it is loaded by path.
"""

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED = ("rehearsal_remote_path", "rehearsal_role_name", "rehearsal_auth_role",
            "rehearsal_fetch_init_container")


@dataclass(frozen=True)
class T9Delivery:
    renderer: object = field(repr=False)

    def remote_path(self, namespace: str) -> str:
        return self.renderer.rehearsal_remote_path(namespace)

    def role_name(self, namespace: str) -> str:
        return self.renderer.rehearsal_role_name(namespace)

    def auth_role(self, namespace: str) -> dict:
        return self.renderer.rehearsal_auth_role(namespace)

    def init_container(self, namespace: str) -> dict:
        return self.renderer.rehearsal_fetch_init_container(namespace)


def load_t9_delivery(render_py: Path) -> T9Delivery:
    """Load the namespace-scoped renderer (T9 #348 or later); an older per-run renderer is refused."""
    spec = importlib.util.spec_from_file_location("t9_target_secrets_render", Path(render_py))
    if spec is None or spec.loader is None:
        raise ValueError("T9 renderer not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if any(not callable(getattr(module, name, None)) for name in REQUIRED):
        raise ValueError("T9 renderer lacks the namespace-scoped rehearsal functions")
    return T9Delivery(module)
