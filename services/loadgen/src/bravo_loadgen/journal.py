"""모든 시도를 한 줄씩 남기는 기록. 대조 리포트의 입력이고, 요청 ID는 여기와 리포트에만 둔다."""

import json
from pathlib import Path
from typing import Self


def message_author(run_id: str) -> str:
    return f"loadgen-{run_id}"


def message_body(run_id: str, logical_id: str) -> str:
    """본문에도 논리 요청 ID를 넣는다. DB의 UNIQUE를 우회한 다른 키의 복사본까지 찾기 위해서다."""
    return f"t18 run={run_id} req={logical_id}"


class Journal:
    def __init__(self, path: Path):
        self._path = Path(path)

    def __enter__(self) -> Self:
        self._file = self._path.open("w", encoding="utf-8")
        return self

    def __exit__(self, *exc_info) -> None:
        self._file.close()

    def write(self, record: dict) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        self._file.flush()


def read_journal(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as file:
        return [json.loads(line) for line in file]
