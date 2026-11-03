import contextlib
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
