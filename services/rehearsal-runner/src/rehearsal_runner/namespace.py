"""리허설 네임스페이스 생성: templates/*.yaml의 ${변수}를 채워 서버 측 apply로 적용한다 (T3 spec D2).

변수는 NAMESPACE와 목적지 프로필(profiles/<환경>.env)의 값이다. 빠진 변수가 있으면 적용하지 않고 실패한다.
"""

import re
from pathlib import Path
from string import Template

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
# 쿠버네티스 네임스페이스 이름 규칙 (DNS label)
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def render(namespace: str, profile: dict[str, str], templates: Path = TEMPLATES) -> str:
    if not _NAME.match(namespace):
        raise ValueError(f"네임스페이스 이름 규칙에 맞지 않는다: {namespace!r}")
    values = {**profile, "NAMESPACE": namespace}
    files = sorted(templates.glob("*.yaml"))
    if not files:
        raise FileNotFoundError(f"템플릿이 없다: {templates}")
    # substitute는 빠진 변수에서 KeyError를 낸다 (safe_substitute와 달리 조용히 넘어가지 않는다)
    return "\n---\n".join(Template(f.read_text(encoding="utf-8")).substitute(values) for f in files)
