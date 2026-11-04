"""시험용 Pod. 실행마다 run_id를 붙여 동시에 돌아도 서로 겹치지 않게 하고, 끝나면 run_id 라벨로 지운다.

- probe: 리허설 네임스페이스 안에서 접속 · 명령을 시도한다
- echo: 리허설 네임스페이스 안의 접속 대상 (양성 대조)
- victim: 정책이 없는 별도 네임스페이스의 Pod. 네임스페이스 밖 대상이자, "정책이 없으면 열리는가" 대조를 하는 곳
"""

import json
from dataclasses import dataclass, field

from .kube import Kubectl, ProbeError

RUN_LABEL = "bravo.dev/isolation-run"
ECHO_PORT = 8080


@dataclass
class Fixtures:
    namespace: str
    run_id: str
    image: str
    victim_uid: str | None = field(default=None, init=False, repr=False)

    @property
    def probe(self) -> str:
        return f"isolation-probe-{self.run_id}"

    @property
    def echo(self) -> str:
        return f"isolation-echo-{self.run_id}"

    @property
    def victim_namespace(self) -> str:
        return f"isolation-victim-{self.run_id}"

    victim = "victim"

    def _labels(self, role: str) -> dict:
        return {RUN_LABEL: self.run_id, "app.kubernetes.io/component": role}

    def pod(self, name: str, namespace: str, role: str, command: list[str]) -> dict:
        """Pod Security restricted를 지키는 최소 Pod. 자원 값은 리허설 LimitRange 안쪽."""
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": self._labels(role)},
            "spec": {
                "automountServiceAccountToken": False,
                "terminationGracePeriodSeconds": 1,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 65534,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "main",
                        "image": self.image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": command,
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "100m", "memory": "64Mi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
            },
        }

    def create(self, kube: Kubectl) -> None:
        serve = ["httpd", "-f", "-p", str(ECHO_PORT)]
        kube.apply(self.pod(self.probe, self.namespace, "probe", ["sleep", "3600"]))
        kube.apply(self.pod(self.echo, self.namespace, "echo", serve))
        kube.apply(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": self.echo,
                    "namespace": self.namespace,
                    "labels": self._labels("echo"),
                },
                "spec": {
                    "selector": self._labels("echo"),
                    "ports": [{"port": ECHO_PORT, "targetPort": ECHO_PORT}],
                },
            }
        )
        created = kube.create(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": self.victim_namespace, "labels": {RUN_LABEL: self.run_id}},
            }
        )
        metadata = created.get("metadata", {})
        uid = metadata.get("uid")
        if (metadata.get("name") != self.victim_namespace or not isinstance(uid, str)
                or not uid.strip()):
            raise ProbeError("시험 namespace 생성 UID 확인 불가")
        self.victim_uid = uid
        kube.apply(self.pod(self.victim, self.victim_namespace, "victim", serve))

    def cleanup(self, kube: Kubectl) -> None:
        """리허설 네임스페이스는 남기고 시험용 리소스만 지운다. 실패해도 다음 정리를 계속한다."""
        selector = f"{RUN_LABEL}={self.run_id}"
        try:
            kube.run(
                "-n", self.namespace, "delete", "pod,service", "-l", selector,
                "--ignore-not-found", "--wait=false",
            )
        finally:
            if self.victim_uid is not None:
                options = {"apiVersion": "v1", "kind": "DeleteOptions",
                           "preconditions": {"uid": self.victim_uid}}
                kube.run("delete", "--raw", f"/api/v1/namespaces/{self.victim_namespace}",
                         "-f", "-", stdin=json.dumps(options))
