import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "infra/envs/onprem/local_vm.py"
spec = importlib.util.spec_from_file_location("local_vm", SCRIPT)
vm_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm_tool)


class LocalVMTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(vm_tool.platform, "system", return_value="Darwin").start()
        patch.object(vm_tool.platform, "machine", return_value="arm64").start()
        self.state = Path(tempfile.mkdtemp(prefix="vm-test-", dir="/tmp")).resolve()
        self.qemu_pid = None
        self.addCleanup(self.cleanup_directory)
        self.vm = self.state / "vm"
        self.vm.mkdir()
        self.launch_id = str(uuid.uuid4())
        (self.vm / "launch.uuid").write_text(self.launch_id)
        (self.vm / "disk.qcow2").write_bytes(b"preserve this disk")
        self.commands = []

    def cleanup_directory(self):
        if self.qemu_pid is not None and vm_tool.process_identity(self.qemu_pid) is not None:
            with vm_tool.qmp(self.vm) as client:
                vm_tool.verify_qmp(client, self.vm, self.launch_id)
                try:
                    client.execute("quit")
                except EOFError:
                    pass
            deadline = time.monotonic() + 5
            while vm_tool.process_identity(self.qemu_pid) is not None:
                if time.monotonic() >= deadline:
                    self.fail("test QEMU still alive; temporary state preserved")
                time.sleep(0.1)
        shutil.rmtree(self.state)

    def process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE,
        )

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(process.stdout.readline(), b"ready\n")
        (self.vm / "qemu.pid").write_text(str(process.pid))
        return process

    def preserved(self):
        self.assertEqual((self.vm / "disk.qcow2").read_bytes(), b"preserve this disk")

    @contextlib.contextmanager
    def server(self, *, wrong_uuid=False, wrong_disk=False, rejected=False,
               disconnect=False, after_quit=lambda: None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.vm / "qmp.sock"))
            server.listen()
            server.settimeout(5)
            errors = []

            def serve():
                try:
                    with server.accept()[0] as connection, connection.makefile("rb") as stream:
                        connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
                        while raw := stream.readline():
                            request = json.loads(raw)
                            command = request["execute"]
                            self.commands.append(command)
                            if command == "quit" and disconnect:
                                break
                            response = {}
                            if command == "query-uuid":
                                response = {"UUID": str(uuid.uuid4()) if wrong_uuid else self.launch_id}
                            elif command == "query-block":
                                response = [{"inserted": {"file": str(self.vm / ("wrong.qcow2" if wrong_disk else "disk.qcow2"))}}]
                            connection.sendall(b'{"event":"STOP"}\r\n')
                            message = {"id": request["id"], "return": response}
                            if rejected and command == "quit":
                                message = {"id": request["id"], "error": {"class": "GenericError"}}
                            connection.sendall(json.dumps(message).encode() + b"\r\n")
                            if command == "quit":
                                after_quit()
                                break
                except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as error:
                    errors.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                yield
            finally:
                thread.join(timeout=6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_invalid_pid_never_reaches_process_control(self):
        for value in ("", "0", "1", "-1", "12\n34", "123x", "2147483648", "9" * 30):
            with self.subTest(pid=value):
                (self.vm / "qemu.pid").write_text(value)
                with patch.object(vm_tool, "process_identity") as inspect, self.assertRaises(ValueError):
                    vm_tool.down(self.state)
                inspect.assert_not_called()
                self.preserved()

    def test_reused_non_qemu_pid_is_not_signaled(self):
        process = self.process()
        with self.assertRaisesRegex(ValueError, "does not belong"):
            vm_tool.down(self.state)
        self.assertIsNone(process.poll())
        self.preserved()

    def test_stale_pid_preserves_all_state(self):
        process = self.process()
        process.terminate()
        process.wait(timeout=5)
        with self.assertRaisesRegex(ValueError, "stale"):
            vm_tool.down(self.state)
        self.preserved()

    def test_legacy_state_without_uuid_is_not_deleted(self):
        (self.vm / "launch.uuid").unlink()
        with self.assertRaises(OSError):
            vm_tool.down(self.state)
        self.preserved()

    def test_fifo_or_link_cannot_substitute_pid_file(self):
        pidfile = self.vm / "qemu.pid"
        os.mkfifo(pidfile)
        with self.assertRaises(ValueError):
            vm_tool.down(self.state)
        pidfile.unlink()
        target = self.state / "other-pid"
        target.write_text("2")
        pidfile.symlink_to(target)
        with self.assertRaises(OSError):
            vm_tool.down(self.state)
        self.preserved()

    def test_state_link_is_not_followed(self):
        target = self.state / "outside"
        self.vm.rename(target)
        self.vm.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            vm_tool.down(self.state)
        self.assertTrue((target / "disk.qcow2").is_file())

    def test_concurrent_lifecycle_command_fails_before_deleting(self):
        with vm_tool.lifecycle_lock(self.state):
            result = subprocess.run([sys.executable, str(SCRIPT), "down", str(self.state)],
                                    capture_output=True, text=True, check=False, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.preserved()

    def test_up_preserves_existing_vm_and_never_starts_another(self):
        with patch.object(vm_tool.subprocess, "run") as run, self.assertRaises(FileExistsError):
            vm_tool.up(self.state)
        run.assert_not_called()
        self.preserved()

    def test_failed_prepare_preserves_launch_state(self):
        shutil.rmtree(self.vm)
        with patch.object(vm_tool.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "prepare")), self.assertRaises(subprocess.CalledProcessError):
            vm_tool.up(self.state)
        self.assertTrue(self.vm.is_dir())
        self.assertEqual(str(uuid.UUID((self.vm / "launch.uuid").read_text().strip())),
                         (self.vm / "launch.uuid").read_text().strip())

    def test_architectures_have_distinct_default_connections(self):
        arm = vm_tool.connection_settings({"arch": "arm64"})
        x86 = vm_tool.connection_settings({"arch": "x86_64"})
        self.assertTrue({arm["ssh_port"], arm["api_port"]}.isdisjoint(
            {x86["ssh_port"], x86["api_port"]}))

    def test_invalid_connection_settings_cannot_create_a_vm(self):
        shutil.rmtree(self.vm)
        for values in ({"BRAVO_VM_ARCH": "riscv64"}, {"BRAVO_SSH_PORT": "0"},
                       {"BRAVO_SSH_PORT": "65536"}, {"BRAVO_SSH_PORT": "2,hostfwd=tcp::22-:22"},
                       {"BRAVO_API_PORT": "abc"}, {"BRAVO_SSH_PORT": "2222", "BRAVO_API_PORT": "2222"}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError):
                    vm_tool.up(self.state)
                self.assertFalse(self.vm.exists())

    def test_busy_port_is_rejected_without_touching_listener_or_disk(self):
        shutil.rmtree(self.vm)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with patch.dict(os.environ, {"BRAVO_SSH_PORT": str(port)}, clear=True), \
                    self.assertRaisesRegex(ValueError, "already in use"):
                vm_tool.up(self.state)
            self.assertFalse(self.vm.exists())
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                listener.accept()[0].close()

    def test_prepare_receives_persisted_architecture_and_custom_ports(self):
        shutil.rmtree(self.vm)
        values = {"BRAVO_VM_ARCH": "x86_64", "BRAVO_SSH_PORT": "52222", "BRAVO_API_PORT": "56443"}
        with patch.dict(os.environ, values, clear=True), patch.object(vm_tool, "available_ports"), \
                patch.object(vm_tool.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "prepare")) as run, \
                self.assertRaises(subprocess.CalledProcessError):
            vm_tool.up(self.state, purpose="compatibility", allow_emulation=True,
                       reason="isolated cross-architecture boot test")
        stored = json.loads((self.vm / "connection.json").read_text())
        self.assertEqual(stored, {"arch": "x86_64", "ssh_port": 52222, "api_port": 56443})
        for key, value in values.items():
            self.assertEqual(run.call_args.kwargs["env"][key], value)

    def test_native_default_uses_actual_host_architecture(self):
        for host, expected in (("arm64", "arm64"), ("aarch64", "arm64"),
                               ("x86_64", "x86_64"), ("amd64", "x86_64")):
            with self.subTest(host=host), patch.object(vm_tool.platform, "machine", return_value=host):
                profile = vm_tool.execution_profile()
                self.assertEqual(profile["guest_arch"], expected)
                self.assertEqual(profile["accelerator"], "hvf")
                self.assertTrue(profile["performance_eligible"])

    def test_cross_architecture_default_fails_before_resources_or_commands(self):
        shutil.rmtree(self.vm)
        with patch.dict(os.environ, {"BRAVO_VM_ARCH": "x86_64"}, clear=True), \
                patch.object(vm_tool, "available_ports") as ports, \
                patch.object(vm_tool.subprocess, "run") as run, \
                self.assertRaisesRegex(ValueError, "cross-architecture"):
            vm_tool.up(self.state)
        ports.assert_not_called()
        run.assert_not_called()
        self.assertFalse(self.vm.exists())

    def test_emulation_needs_all_explicit_compatibility_inputs(self):
        for kwargs in ({}, {"allow_emulation": True},
                       {"purpose": "compatibility", "allow_emulation": True},
                       {"purpose": "performance", "allow_emulation": True, "reason": "test"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                vm_tool.execution_profile("amd64", **kwargs)
        profile = vm_tool.execution_profile("amd64", purpose="compatibility",
                                            allow_emulation=True, reason="boot compatibility only")
        self.assertFalse(profile["performance_eligible"])
        self.assertEqual(profile["accelerator"], "tcg,thread=multi")

    def test_profile_rejects_unsupported_host_and_stale_exception(self):
        with patch.object(vm_tool.platform, "system", return_value="Linux"), self.assertRaises(ValueError):
            vm_tool.execution_profile()
        with patch.object(vm_tool.platform, "machine", return_value="riscv64"), self.assertRaises(ValueError):
            vm_tool.execution_profile()
        with self.assertRaises(ValueError):
            vm_tool.execution_profile("arm64", allow_emulation=True, reason="stale")

    def test_prepare_entry_without_profile_cannot_fetch_or_boot(self):
        result = subprocess.run(["bash", str(SCRIPT.with_name("local-vm.sh")), "_prepare", self.launch_id],
                                env={**os.environ, "BRAVO_VM_STATE": str(self.state), "BRAVO_VM_ARCH": "arm64"},
                                capture_output=True, timeout=5, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.vm / "seed").exists())
        self.preserved()

    def test_forged_native_flag_and_legacy_profile_are_not_measurement_evidence(self):
        with self.assertRaises(OSError):
            vm_tool.measurement(self.state)
        profile = vm_tool.execution_profile("x86_64", purpose="compatibility",
                                            allow_emulation=True, reason="test")
        profile["performance_eligible"] = True
        (self.vm / "execution.json").write_text(json.dumps(profile))
        with self.assertRaisesRegex(ValueError, "profile or host changed"):
            vm_tool.measurement(self.state)
        self.preserved()

    def test_actual_qemu_arguments_must_match_not_just_declared_profile(self):
        profile = vm_tool.execution_profile()
        identity = "Fri Nov 6 03:00:00 2026 /opt/qemu-system-aarch64"
        for command in ("/opt/qemu-system-aarch64 -accel tcg,thread=multi",
                        "/opt/qemu-system-x86_64 -accel hvf",
                        "/opt/qemu-system-aarch64 -accel hvf -accel tcg",
                        "/opt/qemu-system-aarch64 -accel hvf -machine virt,accel=tcg",
                        "/opt/qemu-system-aarch64 -accel"):
            with self.subTest(command=command), \
                    patch.object(vm_tool.subprocess, "check_output", return_value=command), self.assertRaises(ValueError):
                vm_tool.verify_execution(123, identity, profile)
        with patch.object(vm_tool.subprocess, "check_output", return_value="/opt/qemu-system-aarch64 -accel hvf -cpu host"), \
                patch.object(vm_tool, "process_identity", return_value=identity):
            vm_tool.verify_execution(123, identity, profile)

    def test_measurement_requires_same_live_identity_and_excludes_compatibility(self):
        for guest, kwargs in (("arm64", {}), ("x86_64", {
                "purpose": "compatibility", "allow_emulation": True, "reason": "test"})):
            profile = vm_tool.execution_profile(guest, **kwargs)
            (self.vm / "execution.json").write_text(json.dumps(profile))
            with patch.object(vm_tool, "owned_process", return_value=(123, "identity")), \
                    patch.object(vm_tool, "qmp") as connection, \
                    patch.object(vm_tool, "verify_qmp") as verify, \
                    patch.object(vm_tool, "verify_execution") as execution:
                if guest == "arm64":
                    self.assertTrue(vm_tool.measurement(self.state)["performance_eligible"])
                else:
                    with self.assertRaisesRegex(ValueError, "not eligible"):
                        vm_tool.measurement(self.state)
                verify.assert_called_once_with(connection.return_value.__enter__.return_value, self.vm, self.launch_id)
                execution.assert_called_once_with(123, "identity", profile)

    def test_image_architecture_requires_hashed_config_not_index_claim(self):
        layout = self.state / "oci"
        blobs = layout / "blobs" / "sha256"
        blobs.mkdir(parents=True)
        def put(value):
            raw = json.dumps(value).encode()
            digest = hashlib.sha256(raw).hexdigest()
            (blobs / digest).write_bytes(raw)
            return "sha256:" + digest
        wrong_config = put({"architecture": "amd64", "os": "linux"})
        wrong = put({"schemaVersion": 2, "config": {"digest": wrong_config}, "layers": []})
        forged_index = put({"schemaVersion": 2, "manifests": [{"digest": wrong,
                           "platform": {"os": "linux", "architecture": "arm64"}}]})
        with self.assertRaisesRegex(ValueError, "no verified native"):
            vm_tool.image_platform(layout, forged_index, "arm64")
        native_config = put({"architecture": "arm64", "os": "linux"})
        native = put({"schemaVersion": 2, "config": {"digest": native_config}, "layers": []})
        index = put({"schemaVersion": 2, "manifests": [
            {"digest": "sha256:" + "0" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": wrong, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": native, "platform": {"os": "linux", "architecture": "arm64"}}]})
        result = vm_tool.image_platform(layout, index, "aarch64")
        self.assertEqual(result["manifest_digest"], native)
        (blobs / native_config[7:]).write_text('{"architecture":"amd64","os":"linux"}')
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            vm_tool.image_platform(layout, index, "arm64")

    def test_bad_image_blocks_before_vm_or_key_creation(self):
        shutil.rmtree(self.vm)
        with patch.object(vm_tool, "available_ports") as ports, self.assertRaises(ValueError):
            vm_tool.up(self.state, image_layout=self.state, image_digest="latest")
        ports.assert_not_called()
        self.assertFalse(self.vm.exists())

    def test_connection_uses_saved_ports_and_requires_matching_vm_identity(self):
        process = self.process()
        (self.vm / "connection.json").write_text(json.dumps(
            {"arch": "arm64", "ssh_port": 52222, "api_port": 56443}))
        with patch.dict(os.environ, {"BRAVO_VM_ARCH": "arm64", "BRAVO_SSH_PORT": "59999"}), \
                patch.object(vm_tool, "owned_process", return_value=(process.pid, "identity")), self.server():
            self.assertEqual(vm_tool.connection(self.state), 52222)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            vm_tool.connection(self.state)

    def test_connection_rejects_architecture_switch_or_missing_metadata(self):
        with self.assertRaises(OSError):
            vm_tool.connection(self.state)
        (self.vm / "connection.json").write_text(json.dumps(
            {"arch": "arm64", "ssh_port": 52222, "api_port": 56443}))
        with patch.dict(os.environ, {"BRAVO_VM_ARCH": "x86_64"}), \
                patch.object(vm_tool, "owned_process") as inspect, self.assertRaisesRegex(ValueError, "architecture"):
            vm_tool.connection(self.state)
        inspect.assert_not_called()
        self.preserved()

    def test_unknown_architecture_shell_fails_before_state_creation(self):
        empty = self.state / "uncreated"
        result = subprocess.run(["bash", str(SCRIPT.with_name("local-vm.sh")), "up"],
                                env={**os.environ, "BRAVO_VM_ARCH": "unsupported", "BRAVO_VM_STATE": str(empty)},
                                capture_output=True, timeout=5, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(empty.exists())

    def test_qmp_uuid_or_disk_mismatch_never_sends_quit(self):
        process = self.process()
        identity = vm_tool.process_identity(process.pid)
        for options in ({"wrong_uuid": True}, {"wrong_disk": True}):
            with self.subTest(options=options):
                socket_path = self.vm / "qmp.sock"
                if socket_path.exists():
                    socket_path.unlink()
                self.commands.clear()
                with self.server(**options), patch.object(vm_tool, "owned_process", return_value=(process.pid, identity)), self.assertRaises(ValueError):
                    vm_tool.down(self.state)
                self.assertNotIn("quit", self.commands)
                self.assertIsNone(process.poll())
                self.preserved()

    def test_live_process_timeout_and_qmp_failure_preserve_disk(self):
        process = self.process()
        identity = vm_tool.process_identity(process.pid)
        for options in ({}, {"rejected": True}, {"disconnect": True}):
            with self.subTest(options=options):
                socket_path = self.vm / "qmp.sock"
                if socket_path.exists():
                    socket_path.unlink()
                with self.server(**options), patch.object(vm_tool, "owned_process", return_value=(process.pid, identity)), self.assertRaises(ValueError):
                    vm_tool.down(self.state, timeout=0.15)
                self.assertIsNone(process.poll())
                self.preserved()

    def test_changed_process_before_quit_is_not_stopped(self):
        process = self.process()
        with self.server(), patch.object(vm_tool, "owned_process", return_value=(process.pid, "previous process")), self.assertRaisesRegex(ValueError, "changed"):
            vm_tool.down(self.state)
        self.assertNotIn("quit", self.commands)
        self.assertIsNone(process.poll())
        self.preserved()

    def test_exiting_state_requires_same_start_time(self):
        original = "Tue Nov  3 20:43:02 2026 /opt/qemu-system-aarch64"
        for row, system, expected in (
            ("Tue Nov 3 20:43:02 2026 ?E", "Darwin", True),
            ("Tue Nov 3 20:43:02 2026 Z", "Linux", True),
            ("Tue Nov 3 20:43:03 2026 ?E", "Darwin", False),
            ("Tue Nov 3 20:43:02 2026 S", "Darwin", False),
            ("Tue Nov 3 20:43:02 2026 ?E", "Linux", False),
        ):
            with self.subTest(row=row, system=system), \
                    patch.object(vm_tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, row, "")), \
                    patch.object(vm_tool.platform, "system", return_value=system):
                self.assertEqual(vm_tool.process_exiting(100, original), expected)

    def test_exiting_or_reused_identity_never_counts_as_process_absence(self):
        process = self.process()
        identity = vm_tool.process_identity(process.pid)
        for exiting in (True, False):
            socket_path = self.vm / "qmp.sock"
            if socket_path.exists():
                socket_path.unlink()
            identities = iter([identity])  # The pre-quit identity check still matches.
            with self.subTest(exiting=exiting), self.server(), \
                    patch.object(vm_tool, "owned_process", return_value=(process.pid, identity)), \
                    patch.object(vm_tool, "process_identity", side_effect=lambda _, values=identities: next(values, "changed identity")), \
                    patch.object(vm_tool, "process_exiting", return_value=exiting), self.assertRaises(ValueError):
                vm_tool.down(self.state, timeout=0.15)
            self.assertIsNone(process.poll())
            self.preserved()

    def test_delete_only_after_actual_process_exit(self):
        process = self.process()
        identity = vm_tool.process_identity(process.pid)

        def exit_process():
            self.preserved()
            process.terminate()
            process.wait(timeout=5)

        with self.server(after_quit=exit_process), patch.object(vm_tool, "owned_process", return_value=(process.pid, identity)):
            vm_tool.down(self.state)
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.vm.exists())
        vm_tool.down(self.state)  # Repeated cleanup is harmless.

    @unittest.skipUnless(os.environ.get("BRAVO_TEST_QEMU"), "explicit local QEMU integration")
    def test_actual_native_qemu_measurement_guard(self):
        binary = str(Path(os.environ["BRAVO_TEST_QEMU"]).resolve())
        if Path(binary).name != "qemu-system-aarch64":
            self.skipTest("this optional probe is for Apple Silicon native QEMU")
        (self.vm / "execution.json").write_text(json.dumps(vm_tool.execution_profile()))
        command = [binary, "-machine", "virt", "-accel", "hvf", "-cpu", "host",
                   "-nodefaults", "-display", "none", "-S", "-m", "32",
                   "-drive", f"file={self.vm / 'disk.qcow2'},format=raw,if=none",
                   "-uuid", self.launch_id, "-qmp", f"unix:{self.vm / 'qmp.sock'},server=on,wait=off",
                   "-pidfile", str(self.vm / "qemu.pid"), "-daemonize"]
        subprocess.run(command, check=True, capture_output=True, timeout=10)
        self.qemu_pid = int((self.vm / "qemu.pid").read_text())
        result = vm_tool.measurement(self.state)
        self.assertEqual(result["accelerator"], "hvf")
        self.assertTrue(result["performance_eligible"])
        vm_tool.down(self.state)
        self.assertFalse(self.vm.exists())

    @unittest.skipUnless(os.environ.get("BRAVO_TEST_QEMU"), "explicit local QEMU integration")
    def test_actual_qemu_connection_and_exit(self):
        binary = str(Path(os.environ["BRAVO_TEST_QEMU"]).resolve())
        command = [binary, "-machine", "none", "-nodefaults", "-display", "none", "-S", "-m", "32",
                   "-drive", f"file={self.vm / 'disk.qcow2'},format=raw,if=none",
                   "-uuid", self.launch_id, "-qmp", f"unix:{self.vm / 'qmp.sock'},server=on,wait=off",
                   "-pidfile", str(self.vm / "qemu.pid"), "-daemonize"]
        subprocess.run(command, check=True, capture_output=True, timeout=10)
        self.qemu_pid = int((self.vm / "qemu.pid").read_text())
        subprocess.run(["bash", str(SCRIPT.with_name("local-vm.sh")), "down"],
                       env={**os.environ, "BRAVO_VM_STATE": str(self.state)},
                       check=True, capture_output=True, timeout=40)
        self.assertFalse(self.vm.exists())


if __name__ == "__main__":
    unittest.main()
