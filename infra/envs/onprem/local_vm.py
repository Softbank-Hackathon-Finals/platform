"""Serialize local VM lifecycle and stop only the registered QEMU connection."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import time
import uuid
from pathlib import Path


class VMError(ValueError):
    """A safe, actionable lifecycle failure without raw process or QMP data."""


class NotRunning(VMError):
    """The recorded PID is gone or now belongs to something other than this VM's QEMU."""


def architecture(value: str) -> str:
    aliases = {"arm64": "arm64", "aarch64": "arm64", "amd64": "x86_64", "x86_64": "x86_64"}
    if value not in aliases:
        raise VMError("unsupported host or guest architecture")
    return aliases[value]


def execution_profile(guest=None, *, purpose="development", allow_emulation=False, reason=""):
    if platform.system() != "Darwin":
        raise VMError("this local VM launcher requires macOS hardware virtualization")
    host = architecture(platform.machine())
    guest = architecture(guest or host)
    if purpose not in ("development", "performance", "compatibility"):
        raise VMError("unknown VM purpose")
    emulated = host != guest
    if emulated and not (purpose == "compatibility" and allow_emulation and reason.strip()):
        raise VMError("cross-architecture VM refused; build native images, or explicitly select a compatibility-only emulation with a reason")
    if not emulated and (allow_emulation or reason):
        raise VMError("emulation exception supplied for a native VM")
    if len(reason) > 300 or any(ord(c) < 32 for c in reason):
        raise VMError("emulation reason must be a single non-secret line of at most 300 characters")
    return {"schema_version": 1, "host_arch": host, "guest_arch": guest,
            "accelerator": "tcg,thread=multi" if emulated else "hvf",
            "purpose": purpose, "emulation_reason": reason,
            "performance_eligible": not emulated and purpose != "compatibility"}


def saved_profile(vm: Path):
    profile = json.loads(read_regular(vm / "execution.json"))
    expected = execution_profile(profile["guest_arch"], purpose=profile["purpose"],
                                 allow_emulation=profile["host_arch"] != profile["guest_arch"],
                                 reason=profile["emulation_reason"])
    if profile != expected:
        raise VMError("VM execution profile or host changed")
    return profile


def prepare_profile(state: Path):
    """Internal shell entry also rejects the old implicit TCG path before fetch."""
    profile = saved_profile(state / "vm")
    if architecture(os.environ.get("BRAVO_VM_ARCH", platform.machine())) != profile["guest_arch"]:
        raise VMError("prepare architecture differs from approved launch profile")
    return profile


def verify_execution(pid: int, identity: str, profile):
    command = subprocess.check_output(["ps", "-ww", "-p", str(pid), "-o", "args="],
                                      text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
    # ps prints the executable path unquoted on macOS. QEMU's own options do not
    # contain secrets; use the known executable suffix and parse only its args.
    binary = "qemu-system-aarch64" if profile["guest_arch"] == "arm64" else "qemu-system-x86_64"
    fields = identity.split(maxsplit=5)
    if len(fields) != 6 or Path(fields[5]).name != binary or not command.startswith(fields[5] + " "):
        raise VMError("running QEMU architecture differs from launch profile")
    args = shlex.split(command[len(fields[5]) + 1:])
    if args.count("-accel") != 1 or args[-1] == "-accel":
        raise VMError("running accelerator is missing or ambiguous")
    if args[args.index("-accel") + 1] != profile["accelerator"]:
        raise VMError("running accelerator differs from launch profile")
    if any("accel=" in arg for arg in args):
        raise VMError("machine accelerator override is not supported")
    if process_identity(pid) != identity:
        raise VMError("QEMU process changed during execution verification")


def measurement(state: Path):
    vm = state / "vm"
    if vm.is_symlink():
        raise VMError("VM directory must not be a link")
    profile = saved_profile(vm)  # Legacy metadata is UNKNOWN, never inferred native.
    launch_id = read_regular(vm / "launch.uuid").strip()
    pid, identity = owned_process(vm, launch_id)
    with qmp(vm) as client:
        verify_qmp(client, vm, launch_id)
    verify_execution(pid, identity, profile)
    if not profile["performance_eligible"]:
        raise VMError("compatibility/emulated VM is not eligible for performance measurement")
    return {**profile, "vm_uuid": launch_id, "verified_at_unix": time.time(),
            "meaning": "native execution verified; workload readiness and SLO are not verified"}


def image_platform(layout: Path, digest: str, guest: str):
    """Check digest-bound OCI config, not an index label or attestation platform."""
    arch = {"arm64": "arm64", "x86_64": "amd64"}[architecture(guest)]

    def blob(ref):
        if not isinstance(ref, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", ref):
            raise VMError("explicit OCI sha256 digest required")
        path = layout / "blobs" / "sha256" / ref[7:]
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
                raise VMError("invalid OCI metadata blob")
            raw = source.read(16 * 1024 * 1024 + 1)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != ref:
            raise VMError("OCI metadata digest mismatch")
        return json.loads(raw)

    def visit(ref, depth=0):
        if depth > 8:
            raise VMError("OCI index nesting limit exceeded")
        value = blob(ref)
        if value.get("schemaVersion") != 2:
            raise VMError("OCI manifest schemaVersion 2 required")
        if "manifests" in value:
            for item in value["manifests"]:
                declared = item.get("platform")
                if declared is not None and (declared.get("os") != "linux" or declared.get("architecture") != arch):
                    continue
                if found := visit(item["digest"], depth + 1):
                    return found
            return None
        config = blob(value["config"]["digest"])
        if config.get("os") == "linux" and config.get("architecture") == arch:
            return ref
        return None

    found = visit(digest)
    if not found:
        raise VMError("image has no verified native Linux manifest; publish that architecture instead of switching the VM")
    return {"index_digest": digest, "manifest_digest": found, "platform": f"linux/{arch}",
            "meaning": "image architecture only; layers, provenance and runtime acceptance require their existing checks"}


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
        raise NotRunning("stale QEMU PID; state preserved")
    fields = identity.split(maxsplit=5)
    if len(fields) != 6 or not Path(fields[5]).name.startswith("qemu-system-"):
        raise NotRunning("PID does not belong to QEMU")
    command = subprocess.check_output(
        ["ps", "-ww", "-p", str(pid), "-o", "args="], text=True, timeout=5,
        env={**os.environ, "LC_ALL": "C"},
    )
    if any(part not in command for part in (
        f"-uuid {launch_id}", f"-pidfile {vm / 'qemu.pid'}",
        f"file={vm / 'disk.qcow2'},", f"unix:{vm / 'qmp.sock'},",
    )) or process_identity(pid) != identity:
        raise NotRunning("QEMU process does not match this VM")
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


def up(state: Path, *, purpose="development", allow_emulation=False, reason="", image_layout=None, image_digest=None):
    vm = state / "vm"
    # Preserve the old refusal before probing ports or changing any existing state.
    if vm.exists() or vm.is_symlink():
        raise FileExistsError("existing VM state")
    profile = execution_profile(os.environ.get("BRAVO_VM_ARCH"), purpose=purpose,
                                allow_emulation=allow_emulation, reason=reason)
    if bool(image_layout) != bool(image_digest):
        raise VMError("supply both image layout and exact digest")
    if image_layout:
        image_platform(image_layout, image_digest, profile["guest_arch"])
    settings = connection_settings({key: value for key, value in {
        "arch": profile["guest_arch"],
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
    with (vm / "execution.json").open("x", encoding="utf-8") as output:
        json.dump(profile, output)
    script = Path(__file__).with_name("local-vm.sh")
    environment = {**os.environ, "BRAVO_VM_STATE": str(state),
                   "BRAVO_VM_ARCH": settings["arch"],
                   "BRAVO_SSH_PORT": str(settings["ssh_port"]),
                   "BRAVO_API_PORT": str(settings["api_port"])}
    subprocess.run(["bash", str(script), "_prepare", launch_id], env=environment, check=True)
    pid, identity = owned_process(vm, launch_id)
    with qmp(vm) as client:
        verify_qmp(client, vm, launch_id)
    verify_execution(pid, identity, profile)
    if platform.system() == "Darwin":
        # A process-scoped assertion, not a persistent host power-policy change.
        subprocess.Popen(["caffeinate", "-is", "-w", str(pid)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    subprocess.run(["bash", str(script), "_wait"], env=environment, check=True)


def disk_users(disk: Path) -> list[int]:
    """PIDs whose command line opens this disk; QEMU's own image lock is the second guard."""
    listing = subprocess.run(["ps", "-axww", "-o", "pid=", "-o", "args="], capture_output=True,
                             text=True, timeout=5, check=True, env={**os.environ, "LC_ALL": "C"})
    needle = f"file={disk},"
    return [int(line.split(maxsplit=1)[0]) for line in listing.stdout.splitlines() if needle in line]


def preserve_console(vm: Path):
    """QEMU truncates the serial file on boot; keep the earlier boot record beside it."""
    console = vm / "console.log"
    if console.is_symlink():
        raise VMError("console log must not be a link")
    if not console.exists():
        return
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for suffix in range(100):
        kept = vm / f"console-{stamp}{f'-{suffix}' if suffix else ''}.log"
        if not kept.exists() and not kept.is_symlink():
            os.rename(console, kept)
            return
    raise VMError("too many preserved console logs for one second")


def start(state: Path) -> str:
    """Boot the recorded VM again on its own disk, UUID and ports; never create or replace state."""
    vm = state / "vm"
    if vm.is_symlink() or not vm.is_dir():
        raise VMError("no recorded VM to start; use up to create one")
    launch_id = read_regular(vm / "launch.uuid").strip()
    if str(uuid.UUID(launch_id)) != launch_id:
        raise VMError("invalid VM UUID")
    stored = json.loads(read_regular(vm / "connection.json"))
    if not isinstance(stored, dict) or set(stored) != {"arch", "ssh_port", "api_port"}:
        raise VMError("invalid VM connection metadata")
    settings = connection_settings(stored)
    if (vm / "execution.json").is_symlink() or not (vm / "execution.json").is_file():
        raise VMError("VM has no recorded execution profile; UNKNOWN, not restarted")
    profile = saved_profile(vm)
    if settings["arch"] != profile["guest_arch"] or architecture(
            os.environ.get("BRAVO_VM_ARCH", platform.machine())) != profile["guest_arch"]:
        raise VMError("selected architecture does not match the existing VM")
    for name in ("disk.qcow2", "seed.iso"):
        if (vm / name).is_symlink() or not (vm / name).is_file():
            raise VMError(f"existing {name} required; start never creates one")
    if (vm / "qemu.pid").exists() or (vm / "qemu.pid").is_symlink():
        try:
            owned_process(vm, launch_id)
        except NotRunning:
            pass
        else:
            with qmp(vm) as client:
                verify_qmp(client, vm, launch_id)
            return "already running"
    if disk_users(vm / "disk.qcow2"):
        raise VMError("another process already uses this VM disk")
    available_ports(settings)
    preserve_console(vm)
    script = Path(__file__).with_name("local-vm.sh")
    environment = {**os.environ, "BRAVO_VM_STATE": str(state), "BRAVO_VM_ARCH": settings["arch"],
                   "BRAVO_SSH_PORT": str(settings["ssh_port"]),
                   "BRAVO_API_PORT": str(settings["api_port"])}
    subprocess.run(["bash", str(script), "_start", launch_id], env=environment, check=True)
    pid, identity = owned_process(vm, launch_id)
    with qmp(vm) as client:
        verify_qmp(client, vm, launch_id)
    verify_execution(pid, identity, profile)
    if platform.system() == "Darwin":
        subprocess.Popen(["caffeinate", "-is", "-w", str(pid)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    subprocess.run(["bash", str(script), "_wait"], env=environment, check=True)
    return "started"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "start", "down", "connection", "prepare-profile", "measurement"))
    parser.add_argument("state", type=Path)
    parser.add_argument("--purpose", choices=("development", "performance", "compatibility"), default="development")
    parser.add_argument("--allow-emulation", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--image-layout", type=Path)
    parser.add_argument("--image-digest")
    args = parser.parse_args()
    try:
        state = args.state.resolve()
        if args.action == "prepare-profile":
            # Called by _prepare while its parent owns the lifecycle lock.
            print(prepare_profile(state)["accelerator"])
            return
        with lifecycle_lock(state):
            if args.action == "connection":
                print(connection(state))
            elif args.action == "measurement":
                print(json.dumps(measurement(state)))
            elif args.action == "start":
                print(start(state))
            elif args.action == "up":
                up(state, purpose=args.purpose, allow_emulation=args.allow_emulation, reason=args.reason,
                   image_layout=args.image_layout, image_digest=args.image_digest)
            else:
                down(state)
    except VMError as error:
        parser.exit(1, f"VM {args.action} failed: {error}. State preserved.\n")
    except (ValueError, TypeError, KeyError, OSError, EOFError, subprocess.SubprocessError) as error:
        parser.exit(1, f"VM {args.action} failed ({type(error).__name__}): state preserved; inspect VM identity and console before retrying.\n")


if __name__ == "__main__":
    main()
