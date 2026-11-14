"""목적지 프로필(profiles/<환경>.env): 리허설에서 접속을 허용한 목적지 서비스 (ADR-0002, ADR-0034).

허용 대상은 목적지 종류가 정한다. onprem은 클러스터 안의 SeaweedFS · OpenBao다. aws · gcp는
객체 저장소 · 비밀 저장소가 클러스터 밖 API라 라벨로 고를 수 없으므로, 격리 시험(T3)이 그 송신
범위를 확인하기 전까지 `UNSUPPORTED`다(A8 출구 범위와 함께 채운다).
"""

from dataclasses import dataclass
from pathlib import Path

from ..neutral.registry import TARGET_REHEARSAL, Unsupported

TARGET_PREFIXES: dict[str, tuple[str, ...]] = {"onprem": ("SEAWEEDFS", "OPENBAO")}


@dataclass(frozen=True)
class Target:
    name: str
    namespace: str
    label_key: str
    label_value: str
    port: int

    @property
    def selector(self) -> str:
        return f"{self.label_key}={self.label_value}"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"{path}: KEY=VALUE 형식이 아니다: {line}")
        values[key.strip()] = value.strip()
    return values


def targets(values: dict[str, str], kind: str | None = None) -> list[Target]:
    """``kind`` defaults to the profile's ``TARGET_KIND`` (absent → onprem, the original profiles)."""
    kind = kind or values.get("TARGET_KIND", "onprem")
    if kind not in TARGET_PREFIXES:
        raise Unsupported(TARGET_REHEARSAL, kind)
    return [
        Target(
            name=prefix.lower(),
            namespace=values[f"{prefix}_NAMESPACE"],
            label_key=values[f"{prefix}_LABEL_KEY"],
            label_value=values[f"{prefix}_LABEL_VALUE"],
            port=int(values[f"{prefix}_PORT"]),
        )
        for prefix in TARGET_PREFIXES[kind]
    ]
