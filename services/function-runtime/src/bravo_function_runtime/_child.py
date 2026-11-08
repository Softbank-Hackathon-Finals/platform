"""하위 프로세스 진입점: 핸들러 하나를 한 번 부르고 결과를 파일에 쓴다.

작업 입력은 부모가 정한 파일에서 읽고, 결과도 파일로 돌려준다. 핸들러의 print가 결과와 섞이지 않는다.
핸들러를 불러오기 전에 파일 크기 상한을 건다. 출력이 상한을 넘으면 프로세스가 끝나고 부모는 비정상 종료로 본다.
"""

from __future__ import annotations

import importlib
import json
import resource
import sys
import time
import traceback

FILE_SIZE_LIMIT = 16 * 1024 * 1024


class LambdaContext:
    def __init__(self, values: dict):
        self._deadline_ms = values.pop("deadline_ms")
        for key, value in values.items():
            setattr(self, key, value)

    def get_remaining_time_in_millis(self) -> int:
        return max(0, int(self._deadline_ms - time.time() * 1000))


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE_LIMIT, FILE_SIZE_LIMIT))
    try:
        sys.path.insert(0, job["handler_path"])
        handler = getattr(importlib.import_module(job["module"]), job["function"])
        handler(job["event"], LambdaContext(job["context"]))
        result = {"ok": True}
    except Exception as e:  # noqa: BLE001 - 핸들러 오류는 결과로 돌려준다(Lambda와 같은 오류 형식)
        result = {"ok": False, "errorType": type(e).__name__, "errorMessage": str(e)[:1000],
                  "stackTrace": traceback.format_exc().splitlines()[-5:]}
    with open(job["result_path"], "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
