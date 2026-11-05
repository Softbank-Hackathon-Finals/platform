# T19 출발지 원문과 T10 저장 계약의 상호운용 검증

## 확인한 범위

11월 5일 실제 출발지에서 수집한 통제 결과를 **수정하지 않고** 기존 T10
`GateRecords`에 저장·재조회했다. SQLite와 네트워크가 없는 일회용 PostgreSQL에서
동일한 결과를 확인했다. 시험용 계획·사용자·권한은 로컬 fixture다.
운영 계획 등록이나 실제 목적지 인수를 대신하지 않는다.

- 입력: [T14 원문](https://github.com/Softbank-Hackathon-Finals/platform/blob/bce402f67cdf42e58ce10e0ae13d602557318837/docs/evidence/t14-source-runtime-2026-11-05/control-run.json)
- 입력 run: `7b823508-cf4f-47e2-ac5c-0b2ae1fbabf4`
- 입력 지문: `e4663eac582a601a6d864d9d147af113baa7719f363dec4d3aba54d6bd285463`
- 실행 코드: `d495527`의 `services/control-gate/testing/verify_source_staging.py`
- T10 저장 코드: develop `5ba3ff4`에 포함된 `GateRecords`
- 원문 결과: PASS 4개, UNKNOWN 16개. 이 검증에서 결과를 승격하지 않았다.

| 검증 | SQLite | PostgreSQL |
|---|---|---|
| 저장 후 원문·지문 동일 | 통과 | 통과 |
| 같은 입력 재접수 시 한 건 유지 | 통과 | 통과 |
| 권한 없는 저장 | `forbidden` | `forbidden` |
| 출발지 원문을 목적지로 재사용 | `gate_run_scope_mismatch` | `gate_run_scope_mismatch` |
| 목적지 없이 양쪽 결과 결속 | `gate_run_unavailable_or_changed` | `gate_run_unavailable_or_changed` |
| 결속 없이 판정 실행 | `gate_context_unavailable` | `gate_context_unavailable` |
| 판정기 생성 횟수 / 목적지 원문 생성 | 0 / 없음 | 0 / 없음 |

## 재현

현재 저장소의 잠긴 T10 개발 의존성을 설치하고, `PYTHONPATH`에
`services/platform-events/src`, `services/control-gate/src`,
`services/cutover-executor/src`를 추가한다. 위 입력을 별도 보존한 뒤 다음을 실행한다.
출력 경로는 존재하지 않는 개인 경로를 지정한다.

```sh
python services/control-gate/testing/verify_source_staging.py \
  --source-run /private-input/control-run.json \
  --expected-fingerprint e4663eac582a601a6d864d9d147af113baa7719f363dec4d3aba54d6bd285463 \
  --backend sqlite --private-output /private-output/source-staging
```

PostgreSQL은 `--backend postgres`를 사용한다. 서버 도구가 필요하며 기존 서버에
연결하지 않고 fixture가 임시 DB를 만든다. 이번 검증은 PostgreSQL 17이 포함된
로컬 시험 이미지에서 `--network none`, 읽기 전용 소스 마운트, 권한 제거로 실행했다.
운영 자격증명·Docker 소켓은 전달하지 않았다.

## 남은 운영 인수

실제 계획의 등록 환경 ID·출처 권한으로 **새 결과를 수집**하고, 목적지 결과를
같은 계획에 접수해야 한다. 이 원문의 환경 이름을 바꿔 운영 결과로 사용하지 않는다.
승인·증거 신선도·양쪽 필수 시험의 최종 평가는 기존 T19 규칙을 따른다.
출발지 저장 성공만으로 게이트 PASS, 컷오버 허용, T19 완료를 표시하지 않는다.
