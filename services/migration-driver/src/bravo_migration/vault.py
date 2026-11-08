"""Platform-held recovery material: what a person used to keep in an OS credential store.

The OpenBao init response, the OpenBao CA key and the object store identities are created by the
platform and stay with it (운영화 전략 5: 권한은 플랫폼이 쥔다). Each item is written once, 0600, under
a 0700 directory, and is never printed, logged or put in a record. A second write of the same name must
carry the same value: an item that already exists is the recovery material, never replaced.
"""

import json
import os
from pathlib import Path


class VaultError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PlatformVault:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _path(self, name: str) -> Path:
        if not name or not all(c.isalnum() or c in "-_." for c in name) or name.startswith("."):
            raise VaultError("vault_name_invalid", "invalid item name")
        return self.root / f"{name}.json"

    def get(self, name: str) -> dict | None:
        path = self._path(name)
        return json.loads(path.read_text()) if path.exists() else None

    def put_once(self, name: str, value: dict) -> dict:
        """Store `value` unless the item exists; return what is stored (the existing item wins)."""
        path = self._path(name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return json.loads(path.read_text())
        with os.fdopen(fd, "w") as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        return value
