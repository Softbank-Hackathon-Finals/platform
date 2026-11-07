"""하위 프로세스 진입점: 핸들러 하나를 한 번 부르고 결과를 파일에 쓴다.

핸들러의 print가 결과와 섞이지 않도록 결과는 표준 출력이 아니라 부모가 정한 파일로 돌려준다.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import traceback


class LambdaContext:
    def __init__(self, values: dict):
        self._deadline_ms = values.pop("deadline_ms")
        for key, value in values.items():
            setattr(self, key, value)

    def get_remaining_time_in_millis(self) -> int:
        return max(0, int(self._deadline_ms - time.time() * 1000))


def main() -> int:
    job = json.loads(sys.stdin.read())
    result_path = job["result_path"]
    try:
        sys.path.insert(0, job["handler_path"])
        handler = getattr(importlib.import_module(job["module"]), job["function"])
        handler(job["event"], LambdaContext(job["context"]))
        result = {"ok": True}
    except Exception as e:  # noqa: BLE001 - 핸들러 오류는 결과로 돌려준다(Lambda와 같은 오류 형식)
        result = {"ok": False, "errorType": type(e).__name__, "errorMessage": str(e)[:1000],
                  "stackTrace": traceback.format_exc().splitlines()[-5:]}
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
