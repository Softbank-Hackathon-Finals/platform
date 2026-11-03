# bravo-cutover (T12)

Cutover Executor. 상태 머신(T10)의 호출을 받아 쓰기 정지 구간(DB 쓰기를 잠깐 멈추고 쓰기 대상을 출발지에서 목적지로 바꾸는 몇 초)을 8단계 순서대로 실행한다. 순서 · 실패 처리 · 호출 계약은 [설계 문서 8.6](../../docs/design.html), 언어 결정은 [ADR-0006](../../docs/adr/0006-cutover-executor-language.md).

- **쓰기를 받는 DB는 어느 순간에도 최대 하나다.** 상태를 확인할 수 없으면 아무것도 열지 않고 SAFE_HOLD로 멈춘다.
- **LLM을 쓰지 않는다.** 소스 import, 잠금 파일의 전체 의존성, 실제로 import된 모듈을 테스트로 검사한다(`tests/test_no_llm.py`).

## 개발

```bash
uv sync
uv run pytest
uvx ruff check .
uv run uvicorn bravo_cutover.api:create_app --factory --port 8080
```
