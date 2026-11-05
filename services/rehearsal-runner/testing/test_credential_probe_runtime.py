"""The probe's isolated Python runtime needs only the copied standard-library modules."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROBE_FILES = ("__init__.py", "credential_probe.py", "kube_secret_probe.py", "app_health_probe.py",
               "credential_job_probe.py")


def test_probe_runs_without_third_party_packages_and_ignores_pythonpath(tmp_path):
    root = Path(__file__).resolve().parents[1]
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    python = venv / "bin/python"
    purelib = subprocess.check_output([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                      text=True).strip()
    package = Path(purelib) / "rehearsal_runner"
    package.mkdir()
    for name in PROBE_FILES:
        shutil.copyfile(root / "src/rehearsal_runner" / name, package / name)
    malicious = tmp_path / "malicious"
    malicious.mkdir()
    (malicious / "rehearsal_runner.py").write_text('print("unsafe-pythonpath-loaded")')
    result = subprocess.run([str(python), "-I", "-m", "rehearsal_runner.credential_job_probe", "secret-get",
                             "--namespace", "rehearsal-fixture", "--name", "fixture"],
                            env=os.environ | {"PYTHONPATH": str(malicious)}, capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == '{"observation":"unknown"}\n'
    assert result.stderr == ""
