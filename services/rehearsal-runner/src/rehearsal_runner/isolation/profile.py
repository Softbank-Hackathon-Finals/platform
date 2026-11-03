"""목적지 프로필(profiles/<환경>.env): 리허설에서 접속을 허용한 SeaweedFS · OpenBao (ADR-0002)."""

from dataclasses import dataclass
from pathlib import Path


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


def targets(values: dict[str, str]) -> list[Target]:
    return [
        Target(
            name=prefix.lower(),
            namespace=values[f"{prefix}_NAMESPACE"],
            label_key=values[f"{prefix}_LABEL_KEY"],
            label_value=values[f"{prefix}_LABEL_VALUE"],
            port=int(values[f"{prefix}_PORT"]),
        )
        for prefix in ("SEAWEEDFS", "OPENBAO")
    ]
