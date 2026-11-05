"""대조군 Pod 렌더. 거부를 차단 탓으로 돌리려면 차단되지 않은 Pod가 같은 대상에 닿아야 한다.

계획이 대조군 Pod를 요구하므로 계획보다 먼저 띄운다. 비밀과 계정이 없고 잠만 잔다. 적용은 운영자가 한다.
"""

import re
from typing import Any

from .spec import DrillSpec

NAME = "exit-drill-control"
# Python이 들어 있고 이미 장비에 있는 이미지를 쓴다. 저장소 값이 아니라 실행할 때 받는 입력이다.
_PINNED_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_LIFETIME_S = 4 * 3600


class ControlPodError(ValueError):
    """대조군 Pod를 만들 입력이 아니다."""


def render_control_pod(spec: DrillSpec, image: Any) -> dict:
    if not isinstance(image, str) or not _PINNED_IMAGE.fullmatch(image):
        raise ControlPodError("control pod image must be pinned by digest")
    control = next((w for w in spec.workloads.values() if w.role == "control"), None)
    if control is None:
        raise ControlPodError("spec has no control workload")
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": NAME,
            "namespace": spec.namespace,
            # 차단 정책과 같은 레이블이라 되돌릴 때 한 명령으로 함께 지운다.
            "labels": {**control.selector, "component": "target", "app.kubernetes.io/part-of": "bravo-exit-drill"},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "activeDeadlineSeconds": _LIFETIME_S,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 1,
            "securityContext": {
                "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [{
                "name": "control",
                "image": image,
                "imagePullPolicy": "Never",
                "command": ["python", "-c", f"import time; time.sleep({_LIFETIME_S})"],
                "securityContext": {
                    "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "250m", "memory": "128Mi"}},
            }],
        },
    }
