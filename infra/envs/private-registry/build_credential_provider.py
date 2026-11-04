"""Reproduce a reviewed provider candidate on Linux; never install or restart.

Supply the committed provider-build-inputs.json on stdin. Downloads contain only
public source/toolchain artifacts. Credentials are not used by the build.
"""

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

SOURCE_SHA = "1c494fbc3edacf55e2a1e06df21858c03ff9c457"
BINARY_SHA = "83c7897cc39d94502189e5412f70f79fa0dbe64cb7b05b74d10bf955b6930f4a"
ROOT = pathlib.Path("/var/lib/bravo/private-registry")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_inputs(inputs):
    expected = {
        "source_tag": "v1.35.2", "source_commit": SOURCE_SHA,
        "source_archive_url": "https://codeload.github.com/kubernetes/cloud-provider-aws/tar.gz/" + SOURCE_SHA,
        "source_archive_sha256": "b91b1788104cfc02527f175c519843ee825d2e022aeb6b8dc39074e3df77143a",
        "go_version": "go1.26.10",
        "go_linux_amd64_url": "https://go.dev/dl/go1.26.10.linux-amd64.tar.gz",
        "go_linux_amd64_sha256": "ccfe7ae822d2c2f41f4fe7c956199967a4dac22d0ee0d2b2bfdb440aab3b20c5",
        "target": "linux/amd64", "CGO_ENABLED": "0", "GOAMD64": "v1", "buildvcs": False, "trimpath": True,
        "dependency_overrides": {"golang.org/x/net": "v0.60.0", "golang.org/x/text": "v0.42.0"},
        "go_mod_sha256": "9a0081ed5b108989b6c58a2cead2b038c75895a19e6a7bc10bedae7d80fafcf3",
        "go_sum_sha256": "05246487131e44c34233a9b22509c776e3d3995b56d33ed7c297a715300dd3a7",
        "binary_sha256": BINARY_SHA,
        "ldflags": "-w -s -X k8s.io/component-base/version.gitVersion=v1.35.2-bravo.1 -X main.gitVersion=v1.35.2-bravo.1",
    }
    if inputs != expected:
        raise RuntimeError("build_inputs_not_reviewed")


def download(url, destination, expected):
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("xb") as output:
        shutil.copyfileobj(response, output)
    if sha(destination) != expected:
        raise RuntimeError("download_checksum_mismatch")


def main():
    os.umask(0o077)
    inputs = json.load(sys.stdin)
    check_inputs(inputs)
    if os.geteuid() != 0 or os.uname().machine != "x86_64":
        raise RuntimeError("host_platform_invalid")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if ROOT.is_symlink() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077:
        raise RuntimeError("build_directory_not_private")
    candidates = ROOT / "candidates"
    candidates.mkdir(mode=0o700, exist_ok=True)
    if candidates.is_symlink() or candidates.stat().st_uid != 0 or candidates.stat().st_mode & 0o077:
        raise RuntimeError("candidate_directory_not_private")
    candidate = candidates / ("ecr-credential-provider-" + BINARY_SHA)
    if candidate.exists() or candidate.is_symlink():
        if candidate.is_symlink() or sha(candidate) != BINARY_SHA or candidate.stat().st_uid != 0:
            raise RuntimeError("existing_candidate_conflict")
        print(json.dumps({"candidate_verified": True, "binary_sha256": BINARY_SHA, "build_reused": True}))
        return
    with tempfile.TemporaryDirectory(prefix="provider-build-", dir=ROOT) as temporary:
        work = pathlib.Path(temporary)
        download(inputs["source_archive_url"], work / "source.tar.gz", inputs["source_archive_sha256"])
        download(inputs["go_linux_amd64_url"], work / "go.tar.gz", inputs["go_linux_amd64_sha256"])
        for name in ("source.tar.gz", "go.tar.gz"):
            with tarfile.open(work / name) as archive:
                archive.extractall(work, filter="data")
        source = work / ("cloud-provider-aws-" + SOURCE_SHA)
        go = str(work / "go/bin/go")
        # No ambient AWS credentials, proxy, compiler flags or Go overrides.
        env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "GOPATH": str(work / "gopath"),
               "GOCACHE": str(work / "cache"), "GOTOOLCHAIN": "local", "GOENV": "off",
               "GOMAXPROCS": "2", "GOOS": "linux", "GOARCH": "amd64", "GOAMD64": "v1",
               "CGO_ENABLED": "0", "AWS_EC2_METADATA_DISABLED": "true"}
        commands = [
            [go, "get", "golang.org/x/net@v0.60.0", "golang.org/x/text@v0.42.0"],
            [go, "test", "-mod=readonly", "-p", "2", "./cmd/ecr-credential-provider"],
            [go, "build", "-mod=readonly", "-p", "2", "-buildvcs=false", "-trimpath",
             "-ldflags=" + inputs["ldflags"], "-o", str(work / "provider"), "./cmd/ecr-credential-provider"],
        ]
        for index, command in enumerate(commands):
            result = subprocess.run(command, check=False, cwd=source, env=env, capture_output=True, timeout=900)
            if result.returncode:
                raise RuntimeError("candidate_build_phase_" + str(index) + "_failed")
            if sha(source / "go.mod") != inputs["go_mod_sha256"] or sha(source / "go.sum") != inputs["go_sum_sha256"]:
                raise RuntimeError("dependency_closure_changed")
        if sha(work / "provider") != BINARY_SHA:
            raise RuntimeError("candidate_not_reproducible")
        (work / "provider").chmod(0o755)
        os.replace(work / "provider", candidate)
    print(json.dumps({"candidate_verified": True, "binary_sha256": BINARY_SHA, "build_reused": False,
                      "source_commit": SOURCE_SHA, "toolchain": inputs["go_version"],
                      "upstream_package_tests": "pass", "dependency_hashes_match": True,
                      "installed": False, "service_restarted": False}))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired, tarfile.TarError):
        # Build errors contain paths and subprocess output: never publish them.
        print(json.dumps({"candidate_verified": False, "error_code": "candidate_build_failed"}))
        raise SystemExit(1) from None
