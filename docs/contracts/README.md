# 계약의 원본과 연결 지점

상위 구조는 [설계](../design.html), 결정 이유는 [ADR](../adr), 작업 담당·완료 기준은 [작업 계획](../tasks.md)과 연결된 이슈에서 확인한다. 이 문서는 기존 계약의 생산자·소비자를 연결하는 읽기 순서다. 스키마·임계값·현재 진행 상태를 복사하지 않는다. 계약을 바꿀 때 아래 원문과 실제 소비자를 함께 확인한다.

| 경계 | 규칙·형식의 원본 | 생산 → 소비 |
|---|---|---|
| 보안 접근 시험·원본 대조 | [controls](../controls/README.md) | T5 계약 → T14 시험 결과 → T19 게이트 → T10 승인 조건 |
| 데이터·스키마 지문 | [설계 §8.6](../design.html#s8-6), [ADR-0013](../adr/0013-integrity-fingerprint-producer.md) | T13 SQL·규칙 버전 → T12/T20 최종 비교, T19 사전 준비 판정 |
| SHIFT·WATCH 관측 판정 | [observability](../observability/README.md), [ADR-0010](../adr/0010-observability-boundary.md), [T10 창 저장](T10-window-storage.md) | T18 창 요약 → T36 기준 판정 → T17/T10 소비 → T21 표시 |
| 환경·앱·계획 등록 | [등록과 접속·권한 확인](T10-registration.md) | T6/T30 환경 담당의 읽기 참조와 릴리스 출처 → T10 등록 → COLLECT |
| 계획·이벤트·재조회 | [계획 snapshot](T10-plan-snapshot-v1-proposal.md), [이벤트](T10-events-v1-proposal.md), [저장](T10-postgres-storage-v1-proposal.md) | T10 트랜잭션 → T21 조회·SSE |
| 승인·명령·Executor 관측 | [CUTOVER 접수와 상태 읽기](T10-cutover-submit-v1-proposal.md), [운영자 접근](T10-operator-access-v1-proposal.md), [ADR-0011](../adr/0011-cutover-executor-execution-and-failure.md) | T19 정본·T17 SHIFT·호스트 권한 → T10 접수 → T12 실행 → T10 영속 관측 |
| 되돌리기 승인·접수·복귀 | [FAILBACK 승인·접수·복귀](T10-failback-v1-proposal.md), [ADR-0016](../adr/0016-failback-replication-path-check.md) | 운영팀 결정·호스트 권한 → T10 접수 → T12 반대 방향 실행 → T10 증거·Edge(T17) 확인 → T21 표시 |
| S2 리허설·변경 승인 | [리허설 시작·재조회](T10-change-approval.md#리허설을-시작하고-재조회하기), [변경 승인과 반영 접수](T10-change-approval.md) | T15의 확인된 검증 결과 → T10 사람 승인·1회 접수 → T31 반영([ADR-0021](../adr/0021-approved-change-promotion.md), [apply-path](../../services/apply-path/README.md)) → T19 재대조. 미인증 응답은 승인 근거가 아님 |
| 구독 실행 기반·제품 리허설 | [구독 실행 기반](../../operations/subscription/README.md), [T38 #150](https://github.com/Softbank-Hackathon-Finals/platform/issues/150), [T15 #24](https://github.com/Softbank-Hackathon-Finals/platform/issues/24) | T38 VM·OIDC·격리 Job 기반 → T15 제품 템플릿·최대 3회 재시도·diff/digest 승인 결속. 합성 DB_HOST 검증은 T15 제품 인수가 아님 |
| 인프라 접근·자격 | [ADR-0012](../adr/0012-platform-data-access-boundary.md), [platform-data](../../infra/envs/platform-data/README.md) | T7/T30 설치·전달 → 각 런타임 소비. T14의 독립 검증을 대체하지 않음 |

T19의 접근·데이터 준비 판정, T36의 SLI 판정, T12/T13의 최종 무결성은 서로 다른 질문에 답한다. T10은 이 결과와 현재 계획·승인·권한을 결속하며 새 판정기를 만들지 않는다. 관측용 OTel 수집(T37)은 ADR-0010대로 판정 입력과 구분한다.

## 작업 중인 구현을 연결할 때

- [T19 PR #141](https://github.com/Softbank-Hackathon-Finals/platform/pull/141)은 게이트 기준 구현과 생산·소비 포트를 설명한다. 패키지에 고정한 T5 계약 사본은 provenance와 버전 검증을 가진 배포물이다. 파일이 둘 있다는 이유만으로 제거하지 않는다.
- [T36 PR #137](https://github.com/Softbank-Hackathon-Finals/platform/pull/137)의 창 생성·수신·판정을 소비할 때 현재 계획·state·weight·관측 시작 시점의 결속도 확인한다. 공통 함수 호출만으로 입력의 진위를 증명하지 않는다.
- [T10 PR #139](https://github.com/Softbank-Hackathon-Finals/platform/pull/139)은 호스트의 운영자 접근 경계다. T21의 실제 IdP 설정이나 T19 정본 저장소를 독립적으로 정하지 않는다.
- 추가 설계의 전체 발견 경로는 [T29 PR #138](https://github.com/Softbank-Hackathon-Finals/platform/pull/138)의 `docs/additions/README.md` 제안과 연결한다. 이 계약 목차에서 추가 과제 목록을 다시 관리하지 않는다.

위 링크는 구현 검토 경로이며 채택·배포 완료 표가 아니다. 최신 head·리뷰·필수 CI는 PR, 실제 인수는 해당 증빙을 읽는다. 이전 실측 문서는 당시 커밋에 고정해 보존하고, 대체된 계약은 소비 링크를 옮긴 뒤 삭제한다.
