"""Require real Go-binary scan coverage; preserve raw private scanner evidence."""

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile


def summary(report, returncode, binary_sha):
    results = [item for item in report.get("Results", []) if item.get("Type") == "gobinary"]
    packages = sum(len(item.get("Packages", [])) for item in results)
    vulnerabilities = sum(len(item.get("Vulnerabilities", [])) for item in report.get("Results", []))
    if len(results) != 1 or packages <= 0 or returncode not in (0, 1):
        raise ValueError("binary_scan_coverage_missing")
    if (returncode == 0) != (vulnerabilities == 0):
        raise ValueError("scanner_result_exit_mismatch")
    return {"binary_sha256": binary_sha, "scan_mode": "rootfs", "scan_exit_code": returncode,
            "gobinary_result_count": len(results), "package_count": packages,
            "high_critical_fixable_vulnerability_count": vulnerabilities,
            "gate_pass": returncode == 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    binary_sha = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="provider-scan-") as directory:
        work = pathlib.Path(directory)
        staging = work / "root/usr/local/bin"
        staging.mkdir(parents=True)
        binary = staging / "ecr-credential-provider"
        shutil.copyfile(args.binary, binary)
        binary.chmod(0o755)  # Without execute mode this analyzer can report coverage zero.
        (work / "config.yaml").write_text("{}\n")
        (work / "ignore").touch()
        env = {key: value for key, value in os.environ.items() if not key.startswith("TRIVY_")}
        result = subprocess.run([
            "trivy", "--config", str(work / "config.yaml"), "rootfs", "--ignorefile", str(work / "ignore"),
            "--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--ignore-unfixed", "--list-all-pkgs",
            "--exit-code", "1", "--format", "json", "--output", str(args.output.resolve()), str(work / "root"),
        ], check=False, env=env, capture_output=True, timeout=600)
    report = json.loads(args.output.read_text())
    receipt = summary(report, result.returncode, binary_sha)
    print(json.dumps(receipt, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
