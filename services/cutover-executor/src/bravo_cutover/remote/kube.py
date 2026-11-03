"""K8s API: 파드 목록과 파드 안 명령 실행(exec).

쓰기 차단 스위치는 API 서버 프록시로 부를 수 없다. 프록시가 Authorization 헤더를 파드에 넘기지 않아
demo-chat이 401을 낸다(모의 환경 실측). 그래서 파드 안에서 그 파드의 토큰으로 자기 자신을 부른다(spec D4).
필요한 권한: pods list · get, pods/exec, configmaps get · patch(PgBouncer 설정 영구 반영).
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from kubernetes import client, config
from kubernetes.stream import stream

from bravo_cutover.remote import Unconfirmed


class Kube:
    def __init__(self, context: str):
        self._context = context
        self._api = client.CoreV1Api(config.new_client_from_config(context=context))
        # kubernetes.stream.stream은 호출 중에 클라이언트의 request를 바꿔 끼운다. 동시에 exec하려면
        # 스레드마다 클라이언트를 따로 쓴다.
        self._local = threading.local()

    def _exec_api(self) -> client.CoreV1Api:
        if not hasattr(self._local, "api"):
            self._local.api = client.CoreV1Api(config.new_client_from_config(context=self._context))
        return self._local.api

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

    def pod_details(self, namespace: str, selector: str) -> list[dict]:
        """파드 이름 · UID · 재시작 횟수 · Ready · 종료 중 여부. 실행 동안 같은 파드인지 대조할 때 쓴다."""
        try:
            pods = self._api.list_namespaced_pod(namespace, label_selector=selector, _request_timeout=5).items
        except Exception as e:  # noqa: BLE001
            raise Unconfirmed(f"list pods {namespace}/{selector}: {e}") from e
        details = []
        for p in pods:
            conditions = {c.type: c.status for c in (p.status.conditions or [])}
            statuses = p.status.container_statuses or []
            details.append({
                "name": p.metadata.name,
                "uid": p.metadata.uid,
                "restarts": sum(c.restart_count for c in statuses),
                "ready": p.status.phase == "Running" and conditions.get("Ready") == "True",
                "terminating": p.metadata.deletion_timestamp is not None,
            })
        return details

    def read_configmap_versioned(self, namespace: str, name: str, key: str) -> tuple[str, str]:
        try:
            cm = self._api.read_namespaced_config_map(name, namespace, _request_timeout=5)
        except Exception as e:  # noqa: BLE001
            raise Unconfirmed(f"read configmap {namespace}/{name}: {e}") from e
        return cm.data[key], cm.metadata.resource_version

    def read_configmap(self, namespace: str, name: str, key: str) -> str:
        return self.read_configmap_versioned(namespace, name, key)[0]

    def patch_configmap(self, namespace: str, name: str, key: str, value: str,
                        resource_version: str | None = None) -> None:
        """resource_version을 주면 그 사이 다른 쪽이 바꾼 경우 충돌(409)로 실패한다(덮어쓰지 않는다)."""
        body: dict = {"data": {key: value}}
        if resource_version is not None:
            body["metadata"] = {"resourceVersion": resource_version}
        try:
            self._api.patch_namespaced_config_map(name, namespace, body, _request_timeout=5)
        except Exception as e:  # noqa: BLE001
            raise Unconfirmed(f"patch configmap {namespace}/{name}: {e}") from e
        if self.read_configmap(namespace, name, key) != value:
            raise Unconfirmed(f"configmap {namespace}/{name} was not updated")

    def exec(self, namespace: str, pod: str, argv: list[str], timeout_s: float = 10) -> str:
        try:
            resp = stream(
                self._exec_api().connect_get_namespaced_pod_exec, pod, namespace, command=argv,
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

    def _each(self, pods: list[str], arg: str) -> dict[str, bool]:
        # 쓰기 정지 시간 안의 조작이라 파드마다 동시에 부른다
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(pods)))) as pool:
            return dict(zip(pods, pool.map(lambda pod: self._call(pod, arg), pods), strict=True))

    def set(self, blocked: bool) -> dict[str, bool]:
        """모든 실행 중 파드에 적용하고, 각 파드가 응답한 상태를 돌려준다."""
        return self._each(self._kube.running_pods(self._ns, self._selector), "on" if blocked else "off")

    def get(self) -> dict[str, bool]:
        return self._each(self._kube.running_pods(self._ns, self._selector), "get")

    def running(self) -> list[str]:
        return self._kube.running_pods(self._ns, self._selector)

    def pods(self) -> list[str]:
        return self._kube.all_pods(self._ns, self._selector)
