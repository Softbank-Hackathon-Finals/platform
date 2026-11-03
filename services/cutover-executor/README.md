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

실행 설정: `EXECUTOR_DB`(실행 기록 DB), `EXECUTOR_RESOURCES`(환경별 실제 DB 자원 등록 파일).

```json
{"env-aws": {"resource_id": "cnpg-aws/chat", "connection_ref": "secret/source"},
 "env-onprem": {"resource_id": "cnpg-onprem/chat", "connection_ref": "secret/target"}}
```

요청의 `environment_id` · `connection_ref`는 이 등록 정보와 맞아야 하고, lease · epoch는 `resource_id`마다 걸린다. 같은 DB를 다른 환경 이름으로 등록해도 같은 `resource_id`면 겹치는 실행으로 막힌다.

## 무결성 지문 (T13)

쓰기 정지 구간의 최종 비교는 각 DB 안에서 지문을 계산하고 지문만 가져온다(`db_fingerprint.py`). 규칙 · 지원 타입 · 판정 · 연결 방법은 [계약 문서](../../docs/contracts/T13-db-fingerprint-v1.md)에, 시간과 전송량은 [로컬 측정 증거](../../docs/evidence/t13-db-fingerprint-2026-11-04/README.md)에 있다. `uv run python testing/measure_fingerprint.py --out results.json`으로 다시 잴 수 있다.

## 모의 환경 (k3d)

T8(복제) · T30(목적지) 전에 Executor를 시험하는 곳. 클러스터 하나에 출발지 `src` · 목적지 `dst` 네임스페이스를 두고 각각 Postgres 16 · PgBouncer 1.26.1 · demo-chat 2개를 띄운다. 가짜 AWS(moto)는 `aws` 네임스페이스. 복제는 직접 만든 구독으로 흉내 낸다: `dst`의 `bravo_fwd`(src → dst, 켜짐), `src`의 `bravo_rev`(dst → src, 꺼짐).

```bash
python3 testing/mockenv.py up      # 약 2분. demo-chat은 ../../../demo-chat의 origin/main으로 이미지를 만든다
python3 testing/mockenv.py check   # 관리 콘솔 · 복제 · 목적지 앱 → 출발지 DB 쓰기 · 목적지 앱 역할 로그인 거부
python3 testing/mockenv.py down
```

- 비밀번호는 실행마다 새로 만들어 `testing/.state/env.json`(git 제외)에만 둔다.
- PgBouncer 설정은 initContainer가 ConfigMap을 emptyDir로 복사한 파일이다. Executor가 파드 안 파일을 바꾸고 RELOAD한다(spec D3).
- 역할과 권한은 `testing/sql/10-roles.sql` · `30-cutover.sql`. `cutover_control` 권한이 Executor에 필요한 권한 목록의 초안이다(실제 환경은 T6 · T8 담당과 확정).
