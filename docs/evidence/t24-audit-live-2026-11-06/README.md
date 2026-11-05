# T24 플랫폼 감사 로그 실측 — 2026-11-06

플랫폼 AWS 계정에 T24 감사 버킷과 쓰기 · 검색 역할을 실제로 만들고, 팀 코드가 만든 T10 · T11 이벤트를 기록 · 이어 쓰기 · 재전송 · 검증한 뒤 삭제 거부 탐침을 돌렸다. 계정 번호 · 버킷 이름 · 역할 ARN · 버전 ID는 가렸고 원본은 저장소 밖에 보존한다([공개 증빙 절차](../../public-evidence-policy.md)).

## 무엇을 했나

| 단계 | 실행 신원 | 결과 |
|---|---|---|
| 스택 적용(`infra/envs/platform-audit`) | 개발 관리자(SSO) | 13개 생성. 쓰기 역할 접두사 목록 조회 추가로 1개 변경. 적용 뒤 계획 드리프트 없음 |
| T10 이벤트 기록 | 쓰기 역할 | 1건 기록. 입력은 `PlanStore.register_plan`이 만든 `state.changed`(→ COLLECT) |
| T11 이벤트 이어 쓰기 | 쓰기 역할 | 버킷에 저장된 기록에서 체인을 재개해 3건 기록. 입력은 `Tools.dispatch`가 만든 `repo.read` 허용, `rehearsal.exec` 파괴 시도 차단(`bucket_delete`), 미허용 도구 거부 |
| 같은 T11 이벤트 재전송 | 쓰기 역할 | 새 기록 0건, 흡수 3건, chain_digest 그대로 |
| 체인 검증 | 검색 역할 | 4건, `ok`, 마지막 chain_digest 일치 |
| 삭제 거부 탐침(3번째 기록) | 쓰기 역할 | 6종 모두 기대대로 거부, `COMPLIANCE_ACTIVE`, **PASS** |
| 잠긴 버전 삭제 · 보존 단축 · GOVERNANCE 전환 | 개발 관리자(SSO) | 모두 `AccessDenied (object protected by object lock)`. 객체 보존 상태 그대로 |

수치와 digest는 [results.json](results.json), 탐침 원문 형식 그대로의 결과(버킷은 이미 `redacted`)는 [probe-writer.json](probe-writer.json), 측정한 소스 해시는 [source-manifest.json](source-manifest.json)에 있다.

## 확인한 것과 아닌 것

- 확인: 실제 S3 Object Lock(COMPLIANCE 1일) 버킷에 `If-None-Match` 쓰기와 readback, S3에서 읽은 기록으로 체인 재개, `origin_id` 재전송 흡수, 검색 역할의 체인 검증, 쓰기 역할의 삭제 · 단축 · 덮어쓰기 · 정책 변경 · Lock 설정 변경 거부, 관리자도 잠긴 버전을 지우거나 보존을 줄이지 못함.
- 입력 이벤트는 팀 코드(T10 `PlanStore`, T11 `Tools`)가 로컬에서 만든 실제 형식이다. 운영 중인 T10 서비스가 보낸 이벤트는 아니다. T10 서비스의 상시 기록 경로와 `data.tool_events` 배열 분해는 아직 없다.
- 쓰기 역할을 맡을 수 있는 주체는 지금 운영자 SSO 역할뿐이다. T10 런타임 역할은 추가 전이다.
- 기록 객체는 보존 기간(1일) 동안 누구도 지울 수 없다. 정리는 그 뒤 관리자 역할로 한다.
