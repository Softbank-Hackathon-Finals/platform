"""Serialize local VM lifecycle and stop only the registered QEMU connection."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import time
import uuid
from pathlib import Path


class VMError(ValueError):
    """A safe, actionable lifecycle failure without raw process or QMP data."""


@contextlib.contextmanager
def lifecycle_lock(state: Path):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(state / "lifecycle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise VMError("another VM lifecycle command is running") from None
        yield
    finally:
        os.close(descriptor)


def read_regular(path: Path) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise VMError("invalid VM state file")
        return source.read(4097).decode("utf-8")


def process_identity(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
        capture_output=True, text=True, timeout=5, check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode == 1 or not result.stdout.strip():
        try:
            os.kill(pid, 0)  # Existence check only; never signal a PID from disk.
        except ProcessLookupError:
            return None
        raise VMError("process identity unavailable")
    if result.returncode:
        raise VMError("process inspection failed")
    return result.stdout.strip()


def owned_process(vm: Path, launch_id: str) -> tuple[int, str]:
    raw = read_regular(vm / "qemu.pid").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", raw) or not 1 < int(raw) <= 2**31 - 1:
        raise VMError("invalid QEMU PID")
    pid = int(raw)
    identity = process_identity(pid)
    if identity is None:
        raise VMError("stale QEMU PID; state preserved")
    fields = identity.split(maxsplit=5)
    if len(fields) != 6 or not Path(fields[5]).name.startswith("qemu-system-"):
        raise VMError("PID does not belong to QEMU")
    command = subprocess.check_output(
        ["ps", "-ww", "-p", str(pid), "-o", "args="], text=True, timeout=5,
        env={**os.environ, "LC_ALL": "C"},
    )
    if any(part not in command for part in (
        f"-uuid {launch_id}", f"-pidfile {vm / 'qemu.pid'}",
        f"file={vm / 'disk.qcow2'},", f"unix:{vm / 'qmp.sock'},",
    )) or process_identity(pid) != identity:
        raise VMError("QEMU process does not match this VM")
    return pid, identity


class QMP:
    """Bounded QMP exchange; async events never count as command responses."""

    def __init__(self, stream, connection):
        self.stream, self.connection, self.sequence = stream, connection, 0

    def read(self):
        raw = self.stream.readline(1024 * 1024 + 1)
        if not raw:
            raise EOFError("QMP closed")
        if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
            raise VMError("invalid QMP message size")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("invalid QMP message")
        return value

    def execute(self, command: str):
        self.sequence += 1
        self.connection.sendall(json.dumps({"execute": command, "id": self.sequence}).encode() + b"\n")
        deadline = time.monotonic() + 5
        for _ in range(64):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QMP response timed out")
            self.connection.settimeout(remaining)
            message = self.read()
            if "event" in message:
                continue
            if message.get("id") != self.sequence or "error" in message or "return" not in message:
                raise VMError("QMP command failed or response mismatched")
            return message["return"]
        raise VMError("excessive QMP events")


@contextlib.contextmanager
def qmp(vm: Path):
    endpoint = vm / "qmp.sock"
    if not stat.S_ISSOCK(endpoint.lstat().st_mode):
        raise VMError("registered QMP socket required")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(endpoint))
        with connection.makefile("rb") as stream:
            client = QMP(stream, connection)
            if "QMP" not in client.read():
                raise VMError("QMP greeting required")
            client.execute("qmp_capabilities")
            yield client


def verify_qmp(client: QMP, vm: Path, launch_id: str):
    if client.execute("query-uuid") != {"UUID": launch_id}:
        raise VMError("QEMU UUID mismatch")
    blocks = client.execute("query-block")
    if not isinstance(blocks, list) or not any(
        isinstance(row, dict) and isinstance(row.get("inserted"), dict)
        and row["inserted"].get("file") == str(vm / "disk.qcow2") for row in blocks
    ):
        raise VMError("QEMU disk mismatch")


def process_exiting(pid: int, identity: str) -> bool:
    """Darwin can expose a truncated comm while the same process is exiting."""
    result = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
        capture_output=True, text=True, timeout=5, check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    fields = result.stdout.split()
    if result.returncode or len(fields) != 6 or fields[:5] != identity.split(maxsplit=5)[:5]:
        return False
    status = fields[5]
    return status.startswith("Z") or (platform.system() == "Darwin" and "E" in status)


def down(state: Path, *, timeout: float = 30):
    vm = state / "vm"
    if not vm.exists() and not vm.is_symlink():
        return
    if vm.is_symlink() or not vm.is_dir():
        raise VMError("VM directory must not be a link")
    directory = vm.stat()
    launch_id = read_regular(vm / "launch.uuid").strip()
    if str(uuid.UUID(launch_id)) != launch_id:
        raise VMError("invalid VM UUID")
    pid, identity = owned_process(vm, launch_id)
    with qmp(vm) as client:
        verify_qmp(client, vm, launch_id)
        if process_identity(pid) != identity:
            raise VMError("QEMU process changed before shutdown")
        try:
            client.execute("quit")  # Process-bound connection; no kill(PID) race.
        except EOFError:
            pass  # QEMU may close before replying; process exit is still required.
    deadline = time.monotonic() + timeout
    while (current := process_identity(pid)) is not None:
        if time.monotonic() >= deadline:
            raise VMError("QEMU exit not confirmed; state preserved")
        if current != identity and not process_exiting(pid, identity):
            # It may have disappeared between the two ps reads. A different live
            # identity is still rejected; E/Z is never itself proof of absence.
            if process_identity(pid) is not None:
                raise VMError("QEMU process changed during shutdown; state preserved")
            break
        time.sleep(0.1)
    current_directory = vm.lstat()
    if (current_directory.st_dev, current_directory.st_ino) != (directory.st_dev, directory.st_ino):
        raise VMError("VM directory changed; state preserved")
    shutil.rmtree(vm)


def connection_settings(values) -> dict:
    arch = values.get("arch", "arm64")
    if arch not in ("arm64", "x86_64"):
        raise VMError("unsupported VM architecture")
    settings = {"arch": arch}
    for name, default in (("ssh_port", 2222 if arch == "arm64" else 2223),
                          ("api_port", 6443 if arch == "arm64" else 6444)):
        raw = str(values.get(name, default))
        if not re.fullmatch(r"[0-9]{1,5}", raw) or not 1024 <= int(raw) <= 65535:
            raise VMError("VM ports must be integers from 1024 to 65535")
        settings[name] = int(raw)
    if settings["ssh_port"] == settings["api_port"]:
        raise VMError("SSH and API ports must differ")
    return settings


def available_ports(settings):
    # QEMU also binds exclusively; a race after this preflight fails startup safely.
    with contextlib.ExitStack() as stack:
        for name in ("ssh_port", "api_port"):
            listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            try:
                listener.bind(("127.0.0.1", settings[name]))
            except OSError:
                raise VMError(f"{name} is already in use; choose a separate port") from None


def connection(state: Path) -> int:
    vm = state / "vm"
    if vm.is_symlink():
        raise VMError("VM directory must not be a link")
    stored = json.loads(read_regular(vm / "connection.json"))
    if not isinstance(stored, dict) or set(stored) != {"arch", "ssh_port", "api_port"}:
        raise VMError("invalid VM connection metadata")
    settings = connection_settings(stored)
    if settings["arch"] != os.environ.get("BRAVO_VM_ARCH", "arm64"):
        raise VMError("selected architecture does not match the existing VM")
    launch_id = read_regular(vm / "launch.uuid").strip()
    owned_process(vm, launch_id)
    with qmp(vm) as client:
        verify_qmp(client, vm, launch_id)
    return settings["ssh_port"]


def up(state: Path):
    vm = state / "vm"
    # Preserve the old refusal before probing ports or changing any existing state.
    if vm.exists() or vm.is_symlink():
        raise FileExistsError("existing VM state")
    settings = connection_settings({key: value for key, value in {
        "arch": os.environ.get("BRAVO_VM_ARCH", "arm64"),
        "ssh_port": os.environ.get("BRAVO_SSH_PORT"),
        "api_port": os.environ.get("BRAVO_API_PORT"),
    }.items() if value is not None})
    available_ports(settings)
    vm.mkdir(mode=0o700)  # Exclusive: previous state is never reused or deleted.
    launch_id = str(uuid.uuid4())
    with (vm / "launch.uuid").open("x", encoding="utf-8") as output:
        output.write(launch_id + "\n")
    with (vm / "connection.json").open("x", encoding="utf-8") as output:
        json.dump(settings, output)
    script = Path(__file__).with_name("local-vm.sh")
    environment = {**os.environ, "BRAVO_VM_STATE": str(state),
                   "BRAVO_VM_ARCH": settings["arch"],
                   "BRAVO_SSH_PORT": str(settings["ssh_port"]),
                   "BRAVO_API_PORT": str(settings["api_port"])}
    subprocess.run(["bash", str(script), "_prepare", launch_id], env=environment, check=True)
    pid, _ = owned_process(vm, launch_id)
    with qmp(vm) as client:
        verify_qmp(client, vm, launch_id)
    if platform.system() == "Darwin":
        # A process-scoped assertion, not a persistent host power-policy change.
        subprocess.Popen(["caffeinate", "-is", "-w", str(pid)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    subprocess.run(["bash", str(script), "_wait"], env=environment, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "down", "connection"))
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    try:
        state = args.state.resolve()
        with lifecycle_lock(state):
            if args.action == "connection":
                print(connection(state))
            else:
                (up if args.action == "up" else down)(state)
    except VMError as error:
        parser.exit(1, f"VM {args.action} failed: {error}. State preserved.\n")
    except (ValueError, TypeError, OSError, EOFError, subprocess.SubprocessError) as error:
        parser.exit(1, f"VM {args.action} failed ({type(error).__name__}): state preserved; inspect VM identity and console before retrying.\n")


if __name__ == "__main__":
    main()
