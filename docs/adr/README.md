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
| 8.2·8.3 최초 PROPOSE 접수·단일 실행·저장 결과·후보 발행 결속 | [0028 최초 제안 접수와 저장](0028-initial-proposal-records.md) |
| 8.2 계획 진행을 맡는 T10 워크플로 호스트(T10·T31) | [0031 워크플로 호스트](0031-workflow-host.md) |
| 7.5·8.4 리허설 네트워크와 격리 | [0002 송신 허용 범위](0002-rehearsal-egress-allowlist.md), [0003 격리 구현과 시험](0003-rehearsal-namespace-isolation.md) |
| FR-5 · 8.4 리허설 러너의 시도 실행 · 결과 기록 · 판정(T15) | [0023 리허설 러너 시도와 증거](0023-rehearsal-runner-attempt-evidence.md) |
| 8.3 · 8.4 `rehearsal.exec` 동작 범위(정해진 동작 3종)와 실행 위치(API · 도구용 Pod)(T15) | [0024 rehearsal.exec 동작과 실행 위치](0024-rehearsal-exec-actions.md) |
| 8.4 `rehearsal.exec` AI 공간과 채점 공간 분리 · 러너 순서(T15) | [0025 rehearsal.exec 공간 분리와 순서](0025-rehearsal-exec-spaces-and-order.md) |
| 8.4 · 8.6 `rehearsal.exec` 기록 범위와 실패 처리 · 사유 전달(T15) | [0026 rehearsal.exec 기록과 실패 처리](0026-rehearsal-exec-records-and-failures.md) |
| 8.4 시험용 OpenBao 경로의 리허설 쓰기 identity와 잔여 위험(T9·T15) | [0029 리허설 OpenBao 쓰기 identity](0029-rehearsal-openbao-writer.md) |
| 7.3·8.5 보안 결과 형식·인증 | [0001 탐지 결과 형식](0001-security-finding-format.md), [0005 T33 보안 경계](0005-t33-security-boundary.md) |
| 8.5·8.9 등록 앱 식별자와 계획의 평가 프로필(T10·T14·T19) | [0027 계획과 평가 프로필 결속](0027-plan-evaluation-profile.md) |
| 8.5·8.6 GATE 데이터 준비 관찰(Executor, 목적지 시퀀스 올리기만)과 운영자 capture의 readiness 서명(T19·T13) | [0036 GATE 준비 관찰과 capture](0036-gate-readiness-capture.md) |
| 7.5·8.3 플랫폼 자체 서비스의 GitFlow·Helm·Argo CD 배포(T43) | [0029 플랫폼 GitOps 배포](0029-platform-gitops-delivery.md) |
| FR-3 · NFR-7 · 8.3 목적지 IaC 모듈·원하는 상태(Git)·plan API와 에이전트 계획 도구(T44) | [0032 목적지 IaC API](0032-destination-iac-api.md) |
| 8.6 Executor 구현·실행·실패·권한 | [0006 Python 선택](0006-cutover-executor-language.md), [0011 실행 기록과 실패 판정](0011-cutover-executor-execution-and-failure.md), [0013 워크로드 접근](0013-executor-workload-access.md) |
| 8.6 정방향 복제 시간 초과와 방향별 `disable_on_error`(T8) | [0030 복제 시간 초과 30초](0030-t8-replication-timeout.md) |
| 6장 S4 · 8.6 되돌리기 전 복제 경로 확인 | [0016 되돌리기 전 복제 경로 확인](0016-failback-replication-path-check.md) |
| 6장 S4 · 8.2 · 8.7 되돌리기 상태 머신 흐름(승인 · 전용 binding · ABORT 뒤 상태 · Edge만 재시도 · 기록) | [0022 되돌리기 상태 머신 흐름](0022-failback-state-machine-flow.md) |
| 8.6 무결성 지문 생산자·소비자 | [0013 지문 생산자](0013-integrity-fingerprint-producer.md) |
| 8.8·8.11 목적지를 처음 세우는 정형 단계 스택 · 실행기 · T10 승인 결속(T45) | [0033 목적지 처음 구축 실행기](0033-destination-bootstrap-executor.md) |
| 7.5·8.3 tailnet 합류 · 정책 · 분리의 코드화와 Tailscale 워크로드 신원 연합(T48) | [0035 tailnet 신원 코드화](0035-tailnet-identity-as-code.md) |
| 7.2·8.8 이미지 전달 | [0011 비공개 이미지 전달](0011-private-image-delivery.md) |
| 7.2 목적지 개발 앱의 Secrets Manager 호환 (측정용, 출구 증거 아님) | [0017 호환 컨테이너](0017-target-app-secrets-manager-compat.md) |
| 5.2·7.3·8.2·8.7 판정용 관측·진단 지표 | [0010 관측 경계](0010-observability-boundary.md) |
| 7.3 목적지 감사 로그 저장소(T34)·목적지 Collector 분리(T37) | [0016 목적지 감사 로그 저장소](0016-target-audit-store.md) |
| 5절 데모 3 · AUD-01.1을 중앙 전달 전까지 CNPG primary 자체 로그(pods/log)에서 검색(T34·T14) | [0037 AUD-01.1 파드 로그 원천](0037-aud-login-pod-log-source.md) |
| T21 · SOVA MCP 위치·신원·도구 경계·사람 전용 범위 | [0040 SOVA MCP 표면](0040-sova-mcp-surface.md) |
| T21 · 앱 배포 엔진(RAILSHOT 이식)을 별도 파드로, CI는 일회성 러너 | [0048 SOVA Deploy](0048-sova-deploy-railshot-engine.md) |
| T49 · S5 잔존 의존성 지도를 GitOps로 무인 생성(데모: 지도까지, 차단 시험 제외) | [0049 S5 지도 GitOps](0049-s5-residual-map-by-gitops.md) |
| 7.3·8.11 의존성 3등급 규칙과 에이전트 보완 범위(T16·T32) | [0018 의존성 등급 규칙](0018-dependency-grading-rules.md) |
| 7.4·8.3·8.4 플랫폼 감사 로그(실행별 체인 · Object Lock · 삭제 거부 증빙) | [0020 플랫폼 감사 로그](0020-platform-audit-ledger.md) |
| 8.3·8.4 에이전트 도구 계층 경계(소스 경로 · 인벤토리 조회 · 정책 린트 · 파괴 시도 기록) | [0019 에이전트 도구 경계](0019-agent-tool-boundary.md) |
| 7.1 6단계·FR-7 승인 반영(리허설 digest 승격 · diff 재해시 · SAFE_HOLD) | [0021 승인 반영 승격](0021-approved-change-promotion.md) |
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
| 8.4 격리 | 기본 거부·허용 대조와 파괴적 시도 차단을 측정. 목적지 RKE2에서 필수 14개 통과 | k3d(11-03)와 시연 목적지 RKE2(11-06). 시험용 버킷 권한은 별도 검증 | [ADR-0003 결과](0003-rehearsal-namespace-isolation.md#4-결과), T3·T15 |
| 5.3·8.6 쓰기 정지 | 무부하 정상 경로 약 1.0초. 파드별 순차 exec 4.4초에서 동시 실행 약 1.0초 | k3d 모의 환경. 부하 상태 5회나 실제 컷오버 결과가 아님 | [Executor ADR-0011 결과](0011-cutover-executor-execution-and-failure.md#4-결과), T12·T25 |
| 5.3·8.6 DB 지문 비교 | 10만 행·202MB 양쪽 비교의 5회 중앙값 0.631초, 최대 0.658초. 목적지 수신 7,503바이트 | 같은 Mac의 PG18.4. TLS·Tailscale·RKE2·부하 없음. 200MB 이하 실증 계약의 최종 합격을 뜻하지 않음 | [T13 측정](../evidence/t13-db-fingerprint-2026-11-04/README.md), T13·T12 |
| 5.3·8.6 복제 지연 | 로컬 가정 네트워크에서 마지막 행 도착 약 20ms 이내, LSN 일치 약 0.45초. 2배 데이터에서는 더 긴 표본이 있음 | 같은 Mac의 컨테이너. 대역폭·RTT는 가정값이며 실제 경로 아님. 잔여 예산은 나머지 단계 실측값이 아님 | [T8 복제 지연](../evidence/t8-replication-lag-2026-11-04/README.md), T8·T13·T25 |
| 7.3·8.6 첨부 복사 | 컨테이너 포함 시험 86개 통과. 첫 복사와 재실행의 바이트 일치 확인 | 로컬 SeaweedFS·moto·TLS PG. 실제 S3 권한·Tailscale 경로는 미검증 | [T8 첨부 복사](../evidence/t8-attachment-sync-2026-11-04/README.md), T8 |
| 5.3·8.6 원격 DB 왕복과 앱 부하 | 직접·PgBouncer 경로의 고정 RTT 행렬에서 기존 구현과 개선안을 비교. 실패·UNKNOWN·원인 미확인 멈춤을 함께 보존 | 로컬 고정 지연. 실제 클라우드·Cloudflare·TLS 시험 아님. p95 기준 1500ms는 자리값 | [T18 쓰기 잠금 실험](../evidence/t18-write-lock-lab-2026-11-04/README.md), T18·T22·T17 |

실제 경로의 지연, 현재 비교 규칙, 컷오버 전체와 부하 반복은 해당 태스크의 인수 조건을 따른다.
T29의 기록 정리를 위해 이 시험을 새로 실행하거나, 자료가 생긴 뒤 모든 태스크의 종료를 기다리지는 않는다.
측정 자료 안의 PR 상태·연결 상태는 **측정 당시 설명**이다. 현재 상태는 연결된 이슈·PR에서 확인한다.

## 재현한 사례를 추가하거나 정정할 때

같은 실패를 다시 조사할 때는 아래 조건으로 재현하고 결과를 대조한다. 사례는 검증한 환경에만 적용한다.

| 재현 조건 | 관측한 결과와 범위 | 다시 확인할 근거 |
|---|---|---|
| **T10 · 승인 뒤 같은 검증 ID를 보류한다.** 보류(`hold`)와 반영 접수의 커밋 순서를 바꾼다. | 보류가 먼저면 새 접수를 거부했다. 접수가 먼저면 같은 요청은 저장한 `application_id`를 돌려줬다. 같은 계획이 새 버전으로 리허설에 돌아오면 옛 PASS로는 승인할 수 없었다. 시험용 서명과 SQLite·PostgreSQL에서 확인했다. | [수신 계약](../contracts/T10-change-approval.md), [두 커밋 순서와 옛 PASS 회귀 시험](https://github.com/Softbank-Hackathon-Finals/platform/blob/6fce3d54a9c825aaf66140d69211d11d7d5f807e/services/platform-events/tests/test_change_intake.py#L93), [실행 범위와 운영 연결 조건](https://github.com/Softbank-Hackathon-Finals/platform/pull/237) |
| **T10 · 접수 프로세스가 Job 생성 직전·직후에 죽는다.** 원래 키와 다른 키로 같은 작업을 다시 요청한다. | 등록 목적지 RKE2의 시험 Job은 각각 0개·1개였고 추가 제출은 없었다. 생성 직후에는 원 UID를 다시 찾았다. T15 인증 결과가 없어 두 경우 모두 게이트가 닫혔다. | [강제 종료 시점·측정값·재현 절차](../evidence/t10-registered-recovery-2026-11-05/README.md) |
| **T12 · DB 로그인 복구 중 실행기가 교대한다.** 이전 실행기가 보낸 출발지 `LOGIN`을 보류했다가 인계 뒤 전달한다. | 수정 전에는 뒤늦은 `LOGIN`으로 양쪽 DB가 쓰기를 받았다. 수정 뒤에는 정방향·복귀 모두 `SAFE_HOLD`와 두 DB의 lease를 유지했다. 격리 k3s에서 지연 명령 전달 뒤 두 DB에 동시 INSERT를 시도했을 때 원래 명령의 출발지만 커밋했다. | [인계 시 쓰기 규칙](0011-cutover-executor-execution-and-failure.md), [수정 전후 실험](https://github.com/Softbank-Hackathon-Finals/platform/pull/225), [두 lease 유지 회귀 시험](https://github.com/Softbank-Hackathon-Finals/platform/blob/b077554304af65ec40496a01402aec1645f8fa8a/services/cutover-executor/tests/test_runner_fakes.py#L339) |
| **T20 · 컷오버 후 목적지에 쓰고 복귀한다.** DB 복귀가 끝난 뒤 Edge 확인만 같은 키로 재시도한다. | 격리 k3s에서 목적지에 쓴 메시지 5개를 복귀 후 출발지 DB에서 모두 읽었다. DB 복귀 명령은 1회였다. 53회 관측에서 동시 쓰기와 판독 불가는 각각 0건이었다. Edge에는 시험 대역을 썼다. 실제 두 환경의 복귀는 T25에서 측정한다. | [시험한 소스](https://github.com/Softbank-Hackathon-Finals/platform/commit/33c3d0e01d7d1230d7e503cff07fd89e677a3f15), [인수 기록](https://github.com/Softbank-Hackathon-Finals/platform/issues/30#issuecomment-6639280068), [실환경 시험 T25](https://github.com/Softbank-Hackathon-Finals/platform/issues/35) |

해당 태스크 담당자는 재현 조건, 관측한 결과, 환경과 한계를 함께 확인할 수 있을 때 사례를 추가한다. 실행한 커밋과 공개용 시험 기록을 연결한다. 새 결과가 이전 설명을 바꾸면 같은 행에 정정 사유와 새 근거를 남기고 기존 원문 링크를 보존한다. 증빙 공개는 [공개 증빙 절차](../public-evidence-policy.md)를 따른다. 판정 규칙 자체가 바뀌면 해당 계약·ADR에서 별도로 검토한다.

