# 결정 기록과 실측 근거

설계의 선택 이유는 아래 ADR에서 찾는다. 구성과 시나리오는 [설계 문서](../design.html), 계약 필드와 판정 규칙은 [계약 목차](../contracts/README.md)의 원문을 따른다.
상태는 각 ADR 첫머리가 기준이다. 이 목차는 Proposed를 Accepted로 바꾸거나 구현 완료를 판정하지 않는다.
번호가 겹치는 ADR은 파일명까지 구분한다. 기존 파일 경로와 참조는 유지한다.

## 설계와 ADR의 대응

| 설계 범위 | 결정 기록 |
|---|---|
| 1~4장 범위·타겟·원칙, 6장 시나리오, 7장 기반 구성, 8.7·8.8·8.10·8.11, 10장 데모 범위 | [0015 초기 설계의 범위와 기반 구성](0015-initial-design-decisions.md) |
| 8.1·8.3 모델 제공자와 S2 실패 분석 | [0007 모델 제공자와 S2 CI](0007-model-provider-and-s2-ci.md) |
| 8.2·8.9 상태 저장·이벤트·계획 접근 | [0008 상태·이벤트 원자 저장](0008-plan-state-event-transaction.md), [0009 PostgreSQL 상태 저장](0009-postgres-state-adapter.md), [0012 데이터 접근 경계](0012-platform-data-access-boundary.md) |
| 7.5·8.4 리허설 네트워크와 격리 | [0002 송신 허용 범위](0002-rehearsal-egress-allowlist.md), [0003 격리 구현과 시험](0003-rehearsal-namespace-isolation.md) |
| 7.3·8.5 보안 결과 형식·인증 | [0001 탐지 결과 형식](0001-security-finding-format.md), [0005 T33 보안 경계](0005-t33-security-boundary.md) |
| 8.6 Executor 구현·실행·실패·권한 | [0006 Python 선택](0006-cutover-executor-language.md), [0011 실행 기록과 실패 판정](0011-cutover-executor-execution-and-failure.md), [0013 워크로드 접근](0013-executor-workload-access.md) |
| 6장 S4 · 8.6 되돌리기 전 복제 경로 확인 | [0016 되돌리기 전 복제 경로 확인](0016-failback-replication-path-check.md) |
| 8.6 무결성 지문 생산자·소비자 | [0013 지문 생산자](0013-integrity-fingerprint-producer.md) |
| 7.2·8.8 이미지 전달 | [0011 비공개 이미지 전달](0011-private-image-delivery.md) |
| 5.2·7.3·8.2·8.7 판정용 관측·진단 지표 | [0010 관측 경계](0010-observability-boundary.md) |
| 11·12장 브랜치·리뷰·CI 운영 규칙 | [0004 Gitflow](0004-gitflow-task-branches.md), [0005 병합 전 리뷰](0005-review-before-merge.md), [0014 비밀 이력 검사 범위](0014-secret-history-scope.md) |

5.1·5.2의 요구사항은 위 설계 범위로 연결된다. 5.3의 수치는 아래 측정과 함께 읽고, 5.4의 배점·외부 출처는 설계 문서의 원문을 따른다.
11장의 작업 계획과 진행 체크는 [tasks.md](../tasks.md)와 [이슈](https://github.com/Softbank-Hackathon-Finals/platform/issues)가 기준이다.
12장의 미결정 사항은 결정 기록이 있다는 이유만으로 확정하지 않는다.

## 실측 근거

측정 당시 환경·입력·제한은 각 원문의 표와 원자료를 따른다.
아래 값은 이미 기록된 측정의 위치를 알려 주며, 설계 5.3의 실환경 합격선을 확정하지 않는다.
원자료는 고치지 않는다.

| 설계와 관련된 측정 | 결과 요약 | 환경과 해석 범위 | 원문·후속 |
|---|---|---|---|
| 8.4 격리 | 기본 거부·허용 대조와 파괴적 시도 차단을 측정 | k3d. 목적지 RKE2와 실제 시험용 자원은 별도 검증 | [ADR-0003 결과](0003-rehearsal-namespace-isolation.md#4-결과), T3·T15 |
| 5.3·8.6 쓰기 정지 | 무부하 정상 경로 약 1.0초. 파드별 순차 exec 4.4초에서 동시 실행 약 1.0초 | k3d 모의 환경. 부하 상태 5회나 실제 컷오버 결과가 아님 | [Executor ADR-0011 결과](0011-cutover-executor-execution-and-failure.md#4-결과), T12·T25 |
| 5.3·8.6 DB 지문 비교 | 10만 행·202MB 양쪽 비교의 5회 중앙값 0.631초, 최대 0.658초. 목적지 수신 7,503바이트 | 같은 Mac의 PG18.4. TLS·Tailscale·RKE2·부하 없음. 200MB 이하 실증 계약의 최종 합격을 뜻하지 않음 | [T13 측정](../evidence/t13-db-fingerprint-2026-11-04/README.md), T13·T12 |
| 5.3·8.6 복제 지연 | 로컬 가정 네트워크에서 마지막 행 도착 약 20ms 이내, LSN 일치 약 0.45초. 2배 데이터에서는 더 긴 표본이 있음 | 같은 Mac의 컨테이너. 대역폭·RTT는 가정값이며 실제 경로 아님. 잔여 예산은 나머지 단계 실측값이 아님 | [T8 복제 지연](../evidence/t8-replication-lag-2026-11-04/README.md), T8·T13·T25 |
| 7.3·8.6 첨부 복사 | 컨테이너 포함 시험 86개 통과. 첫 복사와 재실행의 바이트 일치 확인 | 로컬 SeaweedFS·moto·TLS PG. 실제 S3 권한·Tailscale 경로는 미검증 | [T8 첨부 복사](../evidence/t8-attachment-sync-2026-11-04/README.md), T8 |
| 5.3·8.6 원격 DB 왕복과 앱 부하 | 직접·PgBouncer 경로의 고정 RTT 행렬에서 기존 구현과 개선안을 비교. 실패·UNKNOWN·원인 미확인 멈춤을 함께 보존 | 로컬 고정 지연. 실제 클라우드·Cloudflare·TLS 시험 아님. p95 기준 1500ms는 자리값 | [T18 쓰기 잠금 실험](../evidence/t18-write-lock-lab-2026-11-04/README.md), T18·T22·T17 |

실제 경로의 지연, 현재 비교 규칙, 컷오버 전체와 부하 반복은 해당 태스크의 인수 조건을 따른다.
T29의 기록 정리를 위해 이 시험을 새로 실행하거나, 자료가 생긴 뒤 모든 태스크의 종료를 기다리지는 않는다.
측정 자료 안의 PR 상태·연결 상태는 **측정 당시 설명**이다. 현재 상태는 연결된 이슈·PR에서 확인한다.

