"""kubectl 호출. 시험은 손으로 따라 칠 수 있는 kubectl 명령만 쓴다."""

import json
import subprocess
from dataclasses import dataclass
from enum import StrEnum

# kubectl exec가 명령은 실행했고 그 명령이 0이 아닌 코드로 끝났을 때 stderr에 남기는 문구
_COMMAND_RAN = "command terminated with exit code"


class Reach(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"


class ProbeError(RuntimeError):
    """시험 명령 자체를 실행하지 못함 (Pod 없음, exec 거부 등). 막힘과 구분한다."""


@dataclass(frozen=True)
class Kubectl:
    context: str | None = None

    def run(self, *args: str, stdin: str | None = None, timeout: int = 60):
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        return subprocess.run(
            [*cmd, *args], input=stdin, capture_output=True, text=True, timeout=timeout
        )

    def apply(self, manifest: dict) -> None:
        out = self.run("apply", "-f", "-", stdin=json.dumps(manifest))
        if out.returncode != 0:
            raise ProbeError(f"apply 실패: {out.stderr.strip()}")

    def get_json(self, *args: str) -> dict:
        out = self.run("get", *args, "-o", "json")
        if out.returncode != 0:
            raise ProbeError(f"get 실패: {out.stderr.strip()}")
        return json.loads(out.stdout)

    def wait_ready(self, namespace: str, pod: str, timeout_s: int = 90) -> bool:
        out = self.run(
            "-n", namespace, "wait", "--for=condition=Ready", f"pod/{pod}",
            f"--timeout={timeout_s}s", timeout=timeout_s + 10,
        )
        return out.returncode == 0

    def exec(self, namespace: str, pod: str, *command: str, timeout: int = 30):
        return self.run("-n", namespace, "exec", pod, "--", *command, timeout=timeout)

    def connect(self, namespace: str, pod: str, host: str, port: int, wait_s: int = 3) -> Reach:
        """pod에서 host:port로 TCP 연결을 시도한다. wait_s 안에 연결되지 않거나 거부되면 BLOCKED."""
        out = self.exec(namespace, pod, "nc", "-z", "-w", str(wait_s), host, str(port))
        return reach_from(out.returncode, out.stderr)


def reach_from(returncode: int, stderr: str) -> Reach:
    """kubectl exec ... nc -z 결과 해석. 명령이 돌지 못한 경우를 막힘으로 착각하지 않는다."""
    if returncode == 0:
        return Reach.OPEN
    if _COMMAND_RAN in stderr:
        return Reach.BLOCKED
    raise ProbeError(f"시험 명령 실행 실패: {stderr.strip() or f'exit {returncode}'}")
