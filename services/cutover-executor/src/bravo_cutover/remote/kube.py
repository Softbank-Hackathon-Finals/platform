"""K8s API: 파드 목록과 파드 안 명령 실행(exec).

쓰기 차단 스위치는 API 서버 프록시로 부를 수 없다. 프록시가 Authorization 헤더를 파드에 넘기지 않아
demo-chat이 401을 낸다(모의 환경 실측). 그래서 파드 안에서 그 파드의 토큰으로 자기 자신을 부른다(spec D4).
필요한 권한: pods list · get, pods/exec.
"""

import json

from kubernetes import client, config
from kubernetes.stream import stream

from bravo_cutover.remote import Unconfirmed


class Kube:
    def __init__(self, context: str):
        self._api = client.CoreV1Api(config.new_client_from_config(context=context))

    def running_pods(self, namespace: str, selector: str) -> list[str]:
        try:
            pods = self._api.list_namespaced_pod(namespace, label_selector=selector, _request_timeout=5).items
        except Exception as e:  # noqa: BLE001
            raise Unconfirmed(f"list pods {namespace}/{selector}: {e}") from e
        return sorted(p.metadata.name for p in pods if p.status.phase == "Running" and not p.metadata.deletion_timestamp)

    def all_pods(self, namespace: str, selector: str) -> list[str]:
        """곧 뜰 파드까지. 진입점 차단이 빠진 파드가 있는지 볼 때 쓴다."""
        try:
            pods = self._api.list_namespaced_pod(namespace, label_selector=selector, _request_timeout=5).items
        except Exception as e:  # noqa: BLE001
            raise Unconfirmed(f"list pods {namespace}/{selector}: {e}") from e
        return sorted(p.metadata.name for p in pods if p.status.phase in ("Pending", "Running"))

    def exec(self, namespace: str, pod: str, argv: list[str], timeout_s: float = 10) -> str:
        try:
            resp = stream(
                self._api.connect_get_namespaced_pod_exec, pod, namespace, command=argv,
                stdout=True, stderr=True, stdin=False, tty=False, _preload_content=False,
            )
            resp.run_forever(timeout=timeout_s)
            out, err = resp.read_stdout(), resp.read_stderr()
            code = resp.returncode
            resp.close()
        except Exception as e:  # noqa: BLE001
            raise Unconfirmed(f"exec {namespace}/{pod}: {e}") from e
        if code != 0:
            raise Unconfirmed(f"exec {namespace}/{pod} exited {code}: {err.strip()[:200]}")
        return out


_SWITCH = """
import json, os, sys, urllib.request
blocked = sys.argv[1]
req = urllib.request.Request("http://127.0.0.1:8080/admin/write-block",
    method="GET" if blocked == "get" else "PUT",
    data=None if blocked == "get" else json.dumps({"blocked": blocked == "on"}).encode(),
    headers={"Authorization": "Bearer " + os.environ["ADMIN_TOKEN"], "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=3).read().decode())
"""


class WriteBlock:
    """demo-chat의 쓰기 차단 스위치(T4). 상태가 프로세스마다 따로 있어 파드마다 부른다."""

    def __init__(self, kube: Kube, namespace: str, selector: str):
        self._kube, self._ns, self._selector = kube, namespace, selector

    def _call(self, pod: str, arg: str) -> bool:
        out = self._kube.exec(self._ns, pod, ["python", "-c", _SWITCH, arg])
        try:
            return bool(json.loads(out)["write_blocked"])
        except (ValueError, KeyError) as e:
            raise Unconfirmed(f"write-block response from {pod}: {out[:200]}") from e

    def set(self, blocked: bool) -> dict[str, bool]:
        """모든 실행 중 파드에 적용하고, 응답으로 받은 상태를 돌려준다."""
        return {pod: self._call(pod, "on" if blocked else "off")
                for pod in self._kube.running_pods(self._ns, self._selector)}

    def get(self) -> dict[str, bool]:
        return {pod: self._call(pod, "get") for pod in self._kube.running_pods(self._ns, self._selector)}

    def pods(self) -> list[str]:
        return self._kube.all_pods(self._ns, self._selector)
