"""Non-secret storage-only probe: separate Pods retain a marker and lock correctly.

No T10 approval, target credential, Kubernetes client or business mutation.
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def run(root, mode, probe_id):
    if not probe_id.isalnum() or len(probe_id) > 64:
        raise ValueError("invalid_probe_id")
    folder = Path(root) / "storage-probes"
    path = folder / (probe_id + ".json")
    if mode == "write":
        folder.mkdir(mode=0o700, exist_ok=True)
        with (folder / ".probe-lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = subprocess.run([sys.executable, "-c",
                ("import fcntl,sys; f=open(sys.argv[1]); "
                 "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"), str(folder / ".probe-lock")],
                capture_output=True, check=False)
            if child.returncode == 0 or b"BlockingIOError" not in child.stderr:
                raise RuntimeError("single_writer_lock_failed")
            raw = json.dumps(dict(probe_id=probe_id, writer_pod_uid=os.environ["POD_UID"],
                synthetic=True, business_mutations=0), sort_keys=True).encode()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    raw = path.read_bytes()
    doc = json.loads(raw)
    assert doc["probe_id"] == probe_id and doc["synthetic"] is True
    assert doc["business_mutations"] == 0
    if mode == "read":
        assert doc["writer_pod_uid"] != os.environ["POD_UID"]
    return dict(mode=mode, probe_id=probe_id, pod_uid=os.environ["POD_UID"],
        writer_pod_uid=doc["writer_pod_uid"], sha256=hashlib.sha256(raw).hexdigest(),
        passed=True, business_mutations=0)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["write", "read"])
    p.add_argument("--root", required=True)
    p.add_argument("--probe-id", required=True)
    a = p.parse_args()
    print(json.dumps(run(a.root, a.mode, a.probe_id)))
