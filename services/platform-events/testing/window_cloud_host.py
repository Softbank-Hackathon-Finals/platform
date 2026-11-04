"""Run a supplied, hashed synthetic test bundle on an already authorized EC2.

No AWS client here. An operator verifies account/instance ownership before SSM
delivery. Input is JSON {files, hashes}; never include secrets or operational data.
Only this run's container ID/label and temporary directory may be removed.
"""

import hashlib
import json
import os
import platform
import pwd
import re
import resource
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

PG_DATA = "/testdata/pg"
PG_OPTIONS = "-c listen_addresses='' -c unix_socket_directories=/socket -c unix_socket_permissions=0777"
DEFAULT_CAPABILITIES = (
    "CHOWN", "DAC_OVERRIDE", "FSETID", "FOWNER", "MKNOD", "NET_RAW", "SETGID",
    "SETUID", "SETFCAP", "SETPCAP", "NET_BIND_SERVICE", "SYS_CHROOT", "KILL", "AUDIT_WRITE",
)


def command(args, *, timeout=45, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError("cloud_harness_command_failed: " + result.stderr[-1200:])
    return result.stdout.strip()


def main():
    os.umask(0o077)
    bundle = json.load(sys.stdin)
    files = bundle["files"]
    hashes = {path: hashlib.sha256(value.encode()).hexdigest() for path, value in files.items()}
    if hashes != bundle["hashes"]:
        raise ValueError("input_hash_mismatch")
    ctr = ["/var/lib/rancher/rke2/bin/ctr", "-a", "/run/k3s/containerd/containerd.sock", "-n", "k8s.io"]
    image = "ghcr.io/cloudnative-pg/postgresql@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185"
    if image not in command([*ctr, "images", "ls", "-q"]).splitlines():
        raise ValueError("postgres_image_missing")
    operation = "t10-window-" + uuid4().hex[:12]
    container_id = None
    report = dict(status="FAIL", operation=operation, postgres_image_id=image,
                  source_sha256=hashes, operational_database_accessed=False,
                  operational_secrets_consumed=False, kubernetes_resources_changed=False,
                  network="none", database_cpu_limit=1, database_memory_mib=512)
    owner = pwd.getpwnam("nobody")
    if owner.pw_uid == 0:
        raise RuntimeError("nonroot_identity_required")
    kubectl = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig", "/etc/rancher/rke2/rke2.yaml"]

    def platform_pods():
        pods = json.loads(command([*kubectl, "get", "pods", "-n", "platform-state", "-o", "json"]))["items"]
        return sorted((p["metadata"]["uid"], sorted(s["restartCount"] for s in p.get("status", {}).get("containerStatuses", []))) for p in pods)

    baseline_pods = platform_pods()
    if not baseline_pods:
        raise RuntimeError("operational_pod_baseline_missing")

    def limit():
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)
        os.nice(10)

    with tempfile.TemporaryDirectory(prefix="bravo-t10-window-", dir="/tmp") as folder:
        root = Path(folder)
        for name, content in files.items():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe_bundle_path")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        socket = root / "socket"
        socket.mkdir(mode=0o777)
        socket.chmod(0o777)
        for path in [root, *root.rglob("*")]:
            os.chown(path, owner.pw_uid, owner.pw_gid)
        env = dict(PATH="/usr/local/bin:/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=str(root / "services/platform-events/src"),
                   AWS_EC2_METADATA_DISABLED="true", PIP_DISABLE_PIP_VERSION_CHECK="1",
                   PIP_NO_CACHE_DIR="1", BRAVO_TEST_SOCKET=str(socket))

        isolated = False

        def run(args, timeout=60):
            if isolated:
                args = ["unshare", "--net", "--", "setpriv", "--reuid", str(owner.pw_uid),
                        "--regid", str(owner.pw_gid), "--clear-groups", *args]
            return subprocess.run(args, cwd=root / "services/platform-events", env=env,
                user=0 if isolated else owner.pw_uid, group=0 if isolated else owner.pw_gid, extra_groups=[],
                capture_output=True, text=True, timeout=timeout, preexec_fn=limit)

        def checked(args, timeout=60):
            result = run(args, timeout)
            if result.returncode:
                report["diagnostic"] = (result.stdout + result.stderr)[-2500:]
                raise RuntimeError("isolated_test_process_failed")
            return result

        try:
            start = ("initdb -D " + PG_DATA + " --auth-local=trust --auth-host=reject --no-locale --encoding=UTF8"
                     " && pg_ctl -D " + PG_DATA + " -l /testdata/server.log -o " + shlex.quote(PG_OPTIONS)
                     + " -w start && exec sleep infinity")
            container_id = "bravo-" + operation
            command([*ctr, "run", "--detach", "--null-io", "--label", "bravo.operation=" + operation,
                "--read-only", "--cpus", "1", "--memory-limit", str(512 * 1024 * 1024),
                *[value for cap in DEFAULT_CAPABILITIES for value in ("--cap-drop", "CAP_" + cap)],
                "--seccomp", "--user", "26:26", "--cwd", "/tmp",
                "--mount", "type=tmpfs,dst=/testdata,options=size=384m:mode=1777",
                "--mount", "type=tmpfs,dst=/tmp,options=size=16m:mode=1777",
                "--mount", f"type=bind,src={socket},dst=/socket,options=rbind:rw",
                image, container_id, "bash", "-c", start])
            info = json.loads(command([*ctr, "containers", "info", container_id]))
            spec = info["Spec"]
            if (not spec["root"]["readonly"] or not spec["process"]["noNewPrivileges"]
                    or spec["process"]["user"]["uid"] != 26
                    or any(spec["process"].get("capabilities", {}).values())
                    or not any(n["type"] == "network" and not n.get("path") for n in spec["linux"]["namespaces"])
                    or spec["linux"]["resources"]["memory"]["limit"] != 512 * 1024 * 1024):
                raise RuntimeError("container_isolation_mismatch")
            report["container_isolation_verified"] = True
            until = time.monotonic() + 40
            while not (socket / ".s.PGSQL.5432").exists():
                if time.monotonic() > until:
                    raise RuntimeError("isolated_postgres_not_ready")
                time.sleep(0.2)
            checked(["python3", "-m", "venv", "--without-pip", str(root / "venv")])
            python = str(root / "venv/bin/python")
            checked([python, "-c", "import urllib.request,json,hashlib,pathlib; m=json.load(urllib.request.urlopen('https://pypi.org/pypi/pip/25.2/json',timeout=15)); i=next(x for x in m['urls'] if x['filename']=='pip-25.2-py3-none-any.whl'); d=urllib.request.urlopen(i['url'],timeout=20).read(4*1024*1024); assert hashlib.sha256(d).hexdigest()==i['digests']['sha256']; pathlib.Path('pip.whl').write_bytes(d)"])
            env["PYTHONPATH"] = os.pathsep.join([str(root / "services/platform-events/pip.whl"),
                str(root / "services/platform-events/src"), str(root / "upstream")])
            checked([python, "-m", "pip", "install", "--quiet", "httpx==0.28.1", "pytest==9.2.0",
                "jsonschema==4.26.0", "rfc3339-validator==0.1.4", "fastapi==0.162.0",
                "psycopg[binary]==3.3.7", "pyjwt[crypto]==2.15.1", "cryptography==50.0.2"], 150)
            isolated = True  # OS network namespace: tests cannot reach IMDS or any remote service.
            identity = checked([python, "-c", "import os; print(os.geteuid())"])
            if int(identity.stdout) != owner.pw_uid:
                raise RuntimeError("test_identity_mismatch")
            result = checked([python, "-m", "pytest", "tests/test_window_store.py", "-q",
                              "-k", "not migration_privileges_and_restart", "-p", "no:cacheprovider"], 90)
            match = re.search(r"(\d+) passed", result.stdout)
            if not match:
                raise RuntimeError("missing_test_count")
            report.update(tests_passed=int(match[1]), tests_deselected=1,
                          pytest_stdout=result.stdout, nonroot_verified=True,
                          python_version=platform.python_version(), test_network_namespace="isolated")
            checked([python, "testing/window_durability.py", "prepare"])
            command([*ctr, "tasks", "exec", "--exec-id", uuid4().hex, "--user", "26:26", container_id,
                     "pg_ctl", "-D", PG_DATA, "-m", "immediate", "-w", "stop"])
            command([*ctr, "tasks", "exec", "--exec-id", uuid4().hex, "--user", "26:26", container_id,
                     "pg_ctl", "-D", PG_DATA, "-l", "/testdata/server.log", "-o", PG_OPTIONS, "-w", "start"])
            durability = json.loads(checked([python, "testing/window_durability.py", "verify"]).stdout)
            report.update(durability=durability, postgres_restart="immediate WAL recovery")
            report["status"] = "PASS"
        except Exception as error:
            report["error_type"] = type(error).__name__
            report["diagnostic"] = report.get("diagnostic", str(error))
        finally:
            if container_id:
                found = subprocess.run([*ctr, "containers", "info", container_id], capture_output=True, text=True, timeout=15)
                if found.returncode == 0:
                    info = json.loads(found.stdout)
                    if info["Labels"].get("bravo.operation") != operation:
                        raise RuntimeError("cleanup_ownership_mismatch")
                    # A failed start can leave a container without a running task.
                    subprocess.run([*ctr, "tasks", "kill", "--signal", "SIGKILL", container_id], capture_output=True, timeout=15)
                    subprocess.run([*ctr, "tasks", "delete", "--force", container_id], capture_output=True, timeout=15)
                    command([*ctr, "containers", "delete", container_id])
                    subprocess.run([*ctr, "snapshots", "remove", container_id], capture_output=True, timeout=15)
                report["container_removed"] = container_id not in command([*ctr, "containers", "ls", "-q"]).splitlines()
                report["snapshot_removed"] = subprocess.run([*ctr, "snapshots", "info", container_id], capture_output=True, timeout=15).returncode != 0
                if not report["container_removed"] or not report["snapshot_removed"]:
                    report["status"] = "FAIL"
    report["temporary_directory_removed"] = not root.exists()
    report["operational_pod_uids_and_restart_counts_unchanged"] = baseline_pods == platform_pods()
    if not report["operational_pod_uids_and_restart_counts_unchanged"]:
        report["status"] = "FAIL"
    print(json.dumps(report))
    return 0 if report["status"] == "PASS" and report["temporary_directory_removed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
