"""시험 결과와 판정. 판정 규칙은 이 파일에만 둔다."""

from dataclasses import asdict, dataclass
from enum import StrEnum


class Kind(StrEnum):
    CONTROL = "control"  # 반드시 성공해야 하는 대조. 실패하면 시험 불가
    ISOLATION = "isolation"  # 반드시 막혀야 하는 시도
    INFO = "info"  # 기록만 하고 판정에서 뺀다 (환경마다 결과가 다른 항목)


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    # 결과를 믿을 수 없음. 예: 막혔는지 보려는데 대상이 정책 없는 곳에서도 응답하지 않음
    INCONCLUSIVE = "inconclusive"


EXIT_OK = 0
EXIT_BREACH = 1
EXIT_UNTESTABLE = 2


@dataclass(frozen=True)
class Result:
    id: str
    title: str
    kind: Kind
    status: Status
    detail: str = ""
    # False면 INCONCLUSIVE를 허용한다 (예: 온프렘에는 메타데이터 서버가 없어 대조할 수 없다)
    required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def exit_code(results: list[Result]) -> int:
    """대조 실패 · 필수 시험의 불확실은 2, 격리 실패는 1, 나머지는 0. 결과가 없으면 2."""
    if not results:
        return EXIT_UNTESTABLE
    if any(r.kind is Kind.CONTROL and r.status is not Status.PASS for r in results):
        return EXIT_UNTESTABLE
    isolation = [r for r in results if r.kind is Kind.ISOLATION]
    if any(r.status is Status.FAIL for r in isolation):
        return EXIT_BREACH
    if any(r.required and r.status is Status.INCONCLUSIVE for r in isolation):
        return EXIT_UNTESTABLE
    return EXIT_OK
