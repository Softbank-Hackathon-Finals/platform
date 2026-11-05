# T27 실제 실행 준비 실측 (2026-11-06)

**[실행 안내](../../operations/t27-exit-drill-real-run.md)의 순서가 임시 k3s에서 끝까지 돌고, 실제 T16 출력이 잔존 지도까지 이어진다. 호환 구성은 PR #210과 함께일 때 비자격으로 끝나고 지도도 나온다. 실제 환경 실행은 아직이다.** 앱 · DB · Tailscale · AWS는 임시 k3s의 대역이고, 실제 클러스터 · VM · AWS에 접속하지 않았다. 그래서 이슈 #32의 완료 기준은 아직 남아 있다.

## 안내 순서를 돌린 결과

`results.json`은 소스 커밋 `f032c5d`, `results-trial-with-pr-210.json`은 그 커밋에 #210(`e528a3b`)을 임시로 합친 값이다. 임시 병합은 저장소에 남기지 않았다.

| 경우 | #210 없음 | #210 임시 병합 | 지도 |
|---|---|---|---|
| 차단 없이 지도만 | `NOT_RUN` | `NOT_RUN` | 남음 5 · 불명 1 · 유지 2 · 차단 대상 아님 1 · 확인 안 됨 4 |
| 앱이 AWS에서 벗어난 경우 | PASS (23개) | PASS (25개) | 확인 안 됨 4가 AWS 경로 막힘 · 앱 동작 확인 4로 바뀐다 |
| 쓰기 대상 입력 없음 | UNKNOWN `CUTOVER_NOT_CONFIRMED` | 같음 | |
| **호환 구성** | **PASS (거짓 통과)** | **UNKNOWN `NOT_EXIT_EVIDENCE`** | #210이 있으면 확인 안 됨 4에 머물고, 맨 위에 막은 항목이 나온다 |
| 되돌리기 | 남은 정책 · 대조군 0, AWS 연결 다시 열림, 복제 유지 | 같음 | |

호환 구성의 차이가 이 작업의 핵심이다. #210 없이는 호환 구성이 정말 PASS로 나와서, 안내에 "그 코드가 없으면 2단계를 돌리지 않는다"고 적었다. 호환 구성은 대역 앱에 표지와 `AWS_ENDPOINT_URL_SECRETS_MANAGER`를 붙여 만들었고, 실제 호환 컨테이너가 응답한 것은 아니다. T30 렌더러의 실제 Pod는 `tests/test_real_target.py`가 렌더 결과로 잰다.

대조군 Pod 매니페스트는 restricted 네임스페이스의 서버 검사를 통과했다(`--dry-run=server`). 잰 시간은 대역 앱 값이라 실제 환경의 예상이 아니다(`seconds`). 지도 표본은 [차단 없음](residual-map-without-block.md)과 [호환 구성](residual-map-compat.md)이다. 인벤토리는 T16 시험 입력이다.

## 실제 이미지로 잰 것

대역으로는 드러나지 않는 것이라 실제 이미지를 따로 띄워 쟀다(`real_pooler_image` · `real_postgres_read`).

| 대상 | 잰 것 | 결과 |
|---|---|---|
| 풀러 이미지 `pgbouncer:1.25.1` | `kill -TERM 1` | **컨테이너가 끝나지 않는다.** 1번 프로세스가 `sh`이고 pgbouncer는 그 자식이다 |
| 〃 | `kill -TERM 1; kill -TERM -1` | 컨테이너가 끝난다 |
| 〃 | bash 접속 시험 | 연결 · 거부 · 시간 초과 · 이름 풀이 실패를 구별한다 |
| PostgreSQL 18 | 복제 상태 읽기 | 구독자 없음 · `streaming` · LSN 증가를 읽고, 읽기 전용 세션이 쓰기를 거부한다. 논리 복제가 아니라 물리 복제 연결로 쟀다 |

## 실제 T16 출력

`tests/test_real_t16.py`가 병합된 T16 코드를 돌려 계획과 지도까지 잇는다. 고정 입력과 항목 · 후보가 같고, 지도와 계획에는 인벤토리의 ARN과 계정 번호가 나오지 않는다. 대체가 없는 SDK(SQS)와 규칙이 못 정한 SDK(Textract)를 넣어도 항목이 지도에 남는다.

실제 demo-chat 소스(`ef0d560`)로도 한 번 돌렸다. 내보내기는 T16 시험 입력을 썼다. 항목 5개의 역할과 등급이 고정 입력과 같았고 지도까지 이어졌다. 이 결과는 파일로 남기지 않았다.

## 준비하며 고친 것

- **풀러 재시작.** 전 안내대로 1번 프로세스에만 신호를 보내면 실제 환경에서 재시작이 일어나지 않고 멈췄을 것이다.
- **Python 없는 이미지.** 풀러 이미지에 Python이 없어 `probe.py`를 돌릴 수 없다. 그 위치는 bash로 같은 상태를 잰다. 모르는 실패는 거부로 세지 않는다.
- **대조군 Pod.** 계획이 대조군을 요구하는데 만드는 길이 없었다. `control-pod`를 더했다.
- **지도.** 판정이 UNKNOWN이어도 이유가 지도에 없었고, T16이 후보에 없다고 한 기능도 빠져 있었다. 둘 다 적는다.
- **대역 안전장치.** 시험용 대조군에 AWS 이름 대역이 없자 장치가 실제 AWS로 나가기 전에 멈췄다. 장치를 고쳤고 실제 매니페스트는 그대로다.

## 재지 못한 것

[실행 안내](../../operations/t27-exit-drill-real-run.md#아직-재지-못한-것)에 적었다. RKE2 · Canal, 실제 demo-chat · PgBouncer 구성 · CNPG, 실제 Tailscale, 실제 AWS 경로, 실제 시간이다.

## 재현과 파일

```sh
cd services/exit-drill
uv run python tests/integration/verify_real_run.py --out <폴더>   # 소스가 커밋돼 있어야 돈다
```

| 파일 | 내용 |
|---|---|
| `results.json` | `f032c5d`로 돌린 결과 · 시간 · 측정 소스 |
| `results-trial-with-pr-210.json` | #210을 임시로 합쳐 돌린 결과 |
| `residual-map-without-block.md` | 차단 없이 뽑은 지도 |
| `residual-map-compat.md` | #210을 합친 호환 구성 판정의 지도 |

`source_revision`과 `source_files`는 측정에 쓴 커밋과 파일 해시이고, 서명이나 제3자 인증을 뜻하지 않는다.
