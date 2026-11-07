"""The site host behind a destination node: the two things only the host can do.

T45's `host-dirs` and `openbao-image` steps need the node's filesystem and its containerd, which the
Kubernetes API does not reach. A site host adapter does exactly those two things and nothing else:
create the declared directories, and import a verified offline image under its declared names.
Local VMs use the canonical generator's `local-vm.sh ssh`; other sites (GCE: infra/envs/gcp-rke2/node.sh)
implement the same two methods.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

CTR = "/var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io"


class HostError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def host_paths_script(paths: list[dict]) -> str:
    """Create each directory exactly as declared. An existing one is accepted only when it is empty and
    already has the declared owner and mode (a re-run after a crash); anything else stops: never reuse data."""
    lines = ["set -eu"]
    for p in paths:
        path, uid, gid, mode = shlex.quote(p["path"]), int(p["uid"]), int(p["gid"]), p["mode"]
        want = f"{uid}:{gid} {mode[1:]}"
        lines.append(
            f'if [ -e {path} ]; then '
            f'[ -d {path} ] && [ -z "$(ls -A {path})" ] && [ "$(stat -c "%u:%g %a" {path})" = "{want}" ] '
            f'|| {{ echo "host_path_in_use {path}" >&2; exit 3; }}; '
            f'else install -d -o {uid} -g {gid} -m {mode} {path}; fi')
        lines.append(f'[ "$(stat -c "%u:%g %a" {path})" = "{want}" ]')
    return "\n".join(lines) + "\n"


class LocalVmHost:
    """A VM made by infra/envs/onprem/local-vm.sh (same disk, UUID and ports as its state directory)."""

    def __init__(self, generator: str | Path, state: str | Path):
        self.generator = Path(generator) / "local-vm.sh"
        self.state = Path(state).expanduser()
        connection = json.loads((self.state / "vm" / "connection.json").read_text())
        self.env = {**os.environ, "BRAVO_VM_STATE": str(self.state),
                    "BRAVO_VM_ARCH": connection["arch"], "BRAVO_SSH_PORT": str(connection["ssh_port"]),
                    "BRAVO_API_PORT": str(connection["api_port"])}

    def _argv(self, command: str) -> list[str]:
        return [str(self.generator), "ssh", command]

    def _ssh(self, command: str, *, stdin=None, timeout_s: float = 120) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(self._argv(command), input=stdin, capture_output=True,
                                  env=self.env, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as e:
            raise HostError("host_timeout", "the site host did not answer in time") from e

    def prepare_paths(self, paths: list[dict]) -> None:
        result = self._ssh("sudo bash -s", stdin=host_paths_script(paths).encode())
        if result.returncode != 0:
            first = result.stderr.decode(errors="replace").strip().splitlines()
            code = "host_path_in_use" if first and first[-1].startswith("host_path_in_use") else "host_paths_failed"
            raise HostError(code, first[-1][:200] if first else "host directory preparation failed")

    def image_names(self) -> set[str]:
        result = self._ssh(f"sudo {CTR} images list -q")
        if result.returncode != 0:
            raise HostError("host_images_unreadable", "containerd image list failed")
        return set(result.stdout.decode().split())

    def import_image(self, oci_tar: str | Path, names: list[str]) -> None:
        """Import an OCI archive whose index names exactly one manifest, then give it every declared name.

        `names` are `repo:tag` and `repo@sha256:<digest>`; the digest must be the archive's manifest digest.
        """
        tagged = [n for n in names if "@" not in n]
        aliases = [n for n in names if "@" in n]
        if len(tagged) != 1 or len(aliases) != 1 or tagged[0].rsplit(":", 1)[0] != aliases[0].split("@", 1)[0]:
            raise HostError("image_names_invalid", "one repo:tag and one repo@sha256 alias of the same repo")
        repo = tagged[0].rsplit(":", 1)[0]
        digest = aliases[0].split("@", 1)[1]
        if self.image_names() >= set(names):
            return
        with open(oci_tar, "rb") as f:
            result = subprocess.run(self._argv(f"sudo {CTR} images import --base-name {shlex.quote(repo)} -"),
                                    stdin=f, capture_output=True, env=self.env, timeout=600, check=False)
        if result.returncode != 0:
            raise HostError("image_import_failed", "containerd import failed")
        # The tag comes from the archive's own ref name; a different declared tag lists nothing here.
        listed = self._ssh(f"sudo {CTR} images list name=={shlex.quote(tagged[0])}").stdout.decode()
        if digest not in listed:
            raise HostError("image_digest_mismatch", "the imported image does not have the declared digest")
        tag_result = self._ssh(f"sudo {CTR} images tag --force {shlex.quote(tagged[0])} {shlex.quote(aliases[0])}")
        if tag_result.returncode != 0 or not self.image_names() >= set(names):
            raise HostError("image_alias_failed", "the digest alias was not created")


class GceRke2Host(LocalVmHost):
    """A GCE node made by infra/envs/gcp-rke2 (terraform). Same two operations, through `node.sh ssh`
    (IAP and OS Login only: no public address, no stored key)."""

    def __init__(self, node_sh: str | Path, node_json: str | Path):
        self.generator = Path(node_sh)
        self.env = {**os.environ, "BRAVO_GCP_NODE": str(Path(node_json).expanduser())}

    def _argv(self, command: str) -> list[str]:
        return [str(self.generator), "ssh", command]
