# T24 follow 실측: T10 이벤트 스트림을 감사 버킷에 잇기 (2026-11-06)

`bravo-audit follow`가 실제 T10 이벤트 API를 구독해 T24 Object Lock 버킷에 기록하고, 다시 실행하면 버킷에 실제로 남은 기록에서 이어 받는지 확인했다. T10 코드는 바꾸지 않았다. 버킷 이름 · 계정 번호 · 역할 ARN · 버전 ID는 가렸고, 원본 출력은 저장소 밖 비공개 경로에 있다([공개 증빙 절차](../../public-evidence-policy.md)).

- 실측 시각: 2026-11-06 07:36:24Z ~ 07:36:30Z (KST 16:36:24 ~ 16:36:30)
- 측정 소스: `04a0846a2f7c764388c7b8ab9a773865c2e68469` (원격 브랜치 `feature/T24-follow-t10-events`에 push한 뒤 그 작업 트리로 실행). 파일 해시는 [source-manifest.json](source-manifest.json)
- 실행 스크립트: [`services/audit-log/testing/follow_live.py`](../../../services/audit-log/testing/follow_live.py). 버킷 · 프로필 · 출력 경로는 인자로 받는다

## 구성

| 구성 | 실제로 쓴 것 |
|---|---|
| T10 | `bravo_platform_events.api.create_app`(변경 없음)을 uvicorn으로 루프백 주소에 띄움. 저장소는 실제 `PlanStore`(로컬 SQLite) |
| T10 권한 | T10이 배포에 맡기는 `Authorizer(request, plan_id)`에, 한 번 쓰고 버린 로컬 자격증명과 `Authorization: Bearer …`를 비교하는 판정을 주입 |
| 이벤트 | 새 계획 하나. `register_plan`(→ COLLECT), `complete_collection`(COLLECT → ANALYZE), `gate.completed` 3건을 먼저 만들고, 첫 follow 뒤 `approval.requested`(cutover) · `execution.updated` 2건을 더 만듦 |
| follow · 기록 | 쓰기 역할(`assumed-role/<name>-writer`), `--once`, 자격증명은 `--auth-token-file` |
| 검증 | 검색 역할(`assumed-role/<name>-reader`)로 기존 `bravo-audit verify s3://…/audit/<plan>/<run>` |

## 결과

| 단계 | 시각(UTC) | 결과 |
|---|---|---|
| follow 1: 첫 따라잡기 | 07:36:24 ~ 07:36:25 | 종료 0. `Last-Event-ID` 없음(저장소 비어 있음). 3건 기록 |
| follow 2: 이어 받기 | 07:36:25 ~ 07:36:27 | 종료 0. 버킷에서 읽은 마지막 T10 이벤트 `…:3`을 `Last-Event-ID`로 보냄. 새 3건만 기록 |
| follow 3: 새 이벤트 없음 | 07:36:27 ~ 07:36:28 | 종료 0. `…:6`을 보냄. 0건 기록, chain_digest 그대로 |
| verify(검색 역할) | 07:36:28 ~ 07:36:30 | 6건, `ok`, chain_digest가 follow 2 · 3의 값과 같음 |

- 기록 6건 모두 readback에서 `COMPLIANCE`, 보존 만료는 쓴 시각 + 1일이다. 버전 ID는 가렸다.
- T10은 모든 요청에서 설정한 인증 헤더를 받았다. follow · verify의 표준출력 · 표준오류 어디에도 자격증명이 없었다(`credential_in_any_output: false`).
- 표준오류에는 `{"follow": "connected", "after_sequence": N}`만 남았다. 이벤트 본문은 출력되지 않았다.

수치 · digest · 단계별 요약은 [results.json](results.json)에 있다. `private_original_sha256`은 비공개 원본 결과 파일의 해시다.

## 측정 뒤 바뀐 동작 (리뷰 #480 반영)

위 실측은 `04a0846a` 기준이다. 리뷰 #480 반영으로 follow는 (1) 마지막으로 기록된 이벤트의 바로 앞(`…:N-1`)을 `Last-Event-ID`로 보내 마지막 이벤트를 다시 받아 흡수되는지 확인하고, (2) `--once`에서 새 이벤트를 받은 연결이 끝나면 한 번 더 연결해 새 이벤트 없이 끝날 때만 종료 0을 낸다. 위 표의 `Last-Event-ID` 값과 연결 횟수는 바뀐 뒤 코드와 다르다. 이번 수정은 실제 버킷으로 다시 실측하지 않았고, 같은 실제 T10 앱을 띄운 시험(`services/audit-log/tests/test_follow.py`)으로 확인했다. 기록되는 객체 · 키 · 체인 형식은 바뀌지 않았다.

## 확인한 것과 아닌 것

- 확인: 실제 T10 앱이 낸 SSE를 실제 버킷에 기록, 재실행 시 버킷의 기록에서 재개(로컬 커서 없음), 중복 · 빈틈 없음, 인증 헤더 전달과 비노출, 검색 역할의 체인 검증.
- 이번 실측에 넣지 않은 것: 상시 모드(`follow=true`) 재연결, 실행 도중 강제 종료 뒤 재시작, 변조 저장소 거부, 403 · 404 · 409 처리. 이들은 같은 실제 T10 앱을 띄운 시험(`services/audit-log/tests/test_follow.py`)으로 확인했다. `data.tool_events` 분해는 T10 스키마 1.0이 그 필드를 받지 않아 실제 T10으로는 만들 수 없고, 같은 형식의 시험 서버로 확인했다.
- T10은 운영 배포가 아니라 같은 코드를 로컬에서 띄운 것이다. follower의 상시 배포, T10 읽기 자격증명 발급, follower 역할의 `writer_principal_arns` 추가는 아직이다.
- 기록 객체는 보존 기간(1일) 동안 누구도 지울 수 없다. 정리는 그 뒤 관리자 역할로 한다.
