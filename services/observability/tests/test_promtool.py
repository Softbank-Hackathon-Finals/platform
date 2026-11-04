"""알림 규칙 · 조회 식의 promtool 단위 시험. 배포와 같은 Prometheus 이미지(digest)로 돈다.

CI 러너에는 Docker가 있어 반드시 돈다. Docker가 없는 로컬에서만 건너뛴다.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = re.search(
    r"image: (prom/prometheus:\S+@sha256:[0-9a-f]{64})",
    (ROOT / "deploy/platform/resources.yaml").read_text(),
).group(1)


def _docker():
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "info"], capture_output=True).returncode == 0:
        return
    if os.environ.get("CI"):
        pytest.fail("CI에서는 promtool 시험을 건너뛸 수 없다: Docker를 쓸 수 없음")
    pytest.skip("Docker 없음(로컬)")


@pytest.mark.parametrize(
    "directory, name",
    [("deploy/platform/rules", "alerts_test.yml"), ("testing", "http_queries_test.yml")],
)
def test_promtool_rules(directory, name):
    _docker()
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
         "-v", f"{ROOT / directory}:/rules:ro", "-w", "/rules",
         "--entrypoint", "/bin/promtool", IMAGE, "test", "rules", name],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUCCESS" in result.stdout + result.stderr
