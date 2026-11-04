# 설계 문서 밖의 추가 작업 · 설계 (이상무)

설계 문서([`docs/design.html`](../design.html), v0.4)에 없는데 이상무가 추가한 작업, 설계, 결정, 구현을 한곳에 모은 목록이다. 2026-11-04 00:04 KST 작성 당시의 기록이다.

- **설계 문서는 고치지 않았다.** 정본은 `design.html` 그대로다. 아래 항목을 설계 문서에 반영할지는 6절의 제안으로 남기고, 설계 문서 담당(배규태, [리뷰 기준](../review-routing.md) 3절)과 정한다.
- **파일은 원래 위치에 둔다.** 다른 작업의 코드 · 문서 · 시험이 지금 경로를 참조하므로 옮기지 않고 링크만 모은다.
- **진행 상황은 이슈가 기준이다.** 아래 상태는 작성 시점의 이슈 체크 수이고, 최신 상태는 [프로젝트 보드](https://github.com/orgs/Softbank-Hackathon-Finals/projects/2)에서 본다.

## 1. 추가한 작업

설계 문서 11장의 작업 목록(T1~T32)에 없던 작업이다. 모두 `docs/tasks.md`에 추가했고 담당은 이상무다.

| 작업 | 우선 | 왜 추가했나 | 설계 문서와의 관계 | 이슈 · 상태 |
|---|---|---|---|---|
| **T33** 보안 탐지 통합: AWS · Google Cloud · 온프렘 탐지를 한 형식 · 한 화면으로 | P1 | 킥오프 요구 "클라우드마다 다른 보안 기능을 통일하고 한 화면에서 시각화"에 대응하는 작업이 설계 문서에 없었다 | 2.1 "보안 · 거버넌스의 통합 관리" 영역을 탐지 결과까지 넓힘 | [#53](https://github.com/Softbank-Hackathon-Finals/platform/issues/53) · 4/7 |
| **T34** 감사 로그 수집: 출발지 · 목적지 | P0 | T5 통제를 구체화하다 찾은 담당 공백. 목적지 감사 로그 수집이 없으면 AUD-01이 계속 UNKNOWN이라 게이트가 PASS될 수 없다 | 8.5 AUD-01, 7.3 "감사 로그" 줄의 구현 | [#56](https://github.com/Softbank-Hackathon-Finals/platform/issues/56) · 0/5 |
| **T35** CNPG 백업 · 복원: 출발지 · 목적지 | P0 | 같은 담당 공백. 백업이 없으면 DR-01이 UNKNOWN이다. 처음 P1로 추가했다가, `full` 게이트가 DR-01을 요구해 P0로 올렸다(PR #51) | 8.5 DR-01 | [#57](https://github.com/Softbank-Hackathon-Finals/platform/issues/57) · 0/4 |
| **T36** 판정용 관측 계약: 오류율 · p95 창 요약과 SHIFT · WATCH 판정 규칙 | P0 | FR-8 단계 판정과 S4 1단계 알림의 측정 위치 · 형식 · "데이터 없음" 처리가 정해져 있지 않았다 | FR-8, 8.2 SHIFT · WATCH, S4 | [#103](https://github.com/Softbank-Hackathon-Finals/platform/issues/103) · 1/5 |
| **T37** 환경 횡단 지표 수집: OpenTelemetry Collector → 플랫폼 지표 저장소 | P1 | 킥오프 테마 예시 "OpenTelemetry 횡단 모니터링"을 맡은 작업이 없었다. 리뷰 기준의 "이상무의 관측(지표 · OTel)" 역할을 작업으로 만든 것 | 2.1 "비용 · 횡단 모니터링: 얕게" | [#104](https://github.com/Softbank-Hackathon-Finals/platform/issues/104) · 0/6 |

## 2. 추가한 설계 문서

### 보안 통제 · 게이트

| 문서 | 내용 | 작업 |
|---|---|---|
| [`docs/controls/README.md`](../controls/README.md) | 6개 통제의 하위 시험 20개, 허용 대조군, UNKNOWN 조건, 게이트 판정 규칙과 검사 순서, 예외 승인의 유효성, 평가 프로필 | T5 |
| [`docs/controls/control-result.schema.json`](../controls/control-result.schema.json) · [`profiles.json`](../controls/profiles.json) · [`evaluation-contexts.json`](../controls/evaluation-contexts.json) · [`contract-cases.json`](../controls/contract-cases.json) | 결과 · 게이트 문서 형식, 평가 프로필(`demo-gate` · `full`), 평가 입력 결속, 계약 시험 | T5 |
| [`docs/controls/regulatory-mapping.md`](../controls/regulatory-mapping.md) | 6개 통제의 일본 · 한국 규제 원문 조항 대응과 그 한계 | T23 |
| [`docs/controls/audit-collection.md`](../controls/audit-collection.md) | 감사 수집과 통제 시험기(T14) 연결, RKE2 감사 정책 | T34 |
| [`docs/controls/backup-restore.md`](../controls/backup-restore.md) | 백업 성공이 아니라 새 클러스터 실제 복원으로 DR-01을 판정하는 조건 | T35 |

### 보안 탐지 통합

| 문서 | 내용 | 작업 |
|---|---|---|
| [`docs/security/detection-integration.md`](../security/detection-integration.md) | 출처별 탐지(Security Hub · GuardDuty · SCC · Kyverno · Falco · 통제 결과)를 OCSF 부분집합으로 통합하는 설계 | T33 |
| [`docs/security/detection-hardening.md`](../security/detection-hardening.md) | 탐지 API의 보안 경계와 운영 인수 조건 | T33 |
| [`docs/security/detection/`](../security/detection/) | 정규화 형식 스키마, 출처별 매핑 규칙, 원본 · 정규화 예시 | T33 |

### 관측

| 문서 | 내용 | 작업 |
|---|---|---|
| [`docs/observability/README.md`](../observability/README.md) | 판정 입력을 바깥에서 재는 원칙, SHIFT · WATCH 판정 규칙, 창 요약 생산 규칙, 관측 데이터 보안 규칙 | T36 |
| [`docs/observability/`](../observability/)의 스키마 · 기준 프로필 · 계약 시험 | `SliWindow` · `ThresholdProfiles` · `SliJudgement`, `demo-shift` v1(제안값), 판정 25건 · 거부 8건 | T36 |

### 추후 고려사항 (현재 작업 범위를 바꾸지 않음)

| 문서 | 내용 |
|---|---|
| [`docs/security/incident-based-architecture.md`](../security/incident-based-architecture.md) | 실제 사이버공격 사례 기반 멀티클라우드 보안 아키텍처 설계 |
| [`docs/security/continuous-secure-modernization.md`](../security/continuous-secure-modernization.md) | Patching as a Service 기반 지속적 보안 개선 및 멀티클라우드 마이그레이션 아키텍처 |

## 3. 설계 제안 기록 (ADR)

아래 세 ADR은 모두 원문에서 **Proposed(제안)** 상태다. 병합된 구현 이력은 4절에 따로 기록하며, 구현이 존재한다는 이유로 ADR이 채택됐다고 해석하지 않는다. 채택 여부는 각 ADR의 상태를 따른다.

| ADR | 제안 내용 | 작업 |
|---|---|---|
| [ADR-0001](../adr/0001-security-finding-format.md) | 탐지 결과 형식은 OCSF 부분집합 | T33 |
| [ADR-0005 (T33)](../adr/0005-t33-security-boundary.md) | T33의 범위 제한 인증과 실패 보존 | T33 |
| [ADR-0010](../adr/0010-observability-boundary.md) | 판정 입력은 평가 대상 밖에서 재고, 로그는 각 환경 안에 두고, 지표는 OpenTelemetry로 플랫폼에 모은다 | T36 · T37 |

## 4. 구현

| 위치 | 내용 | 작업 |
|---|---|---|
| [`services/security-findings`](../../services/security-findings/) | 탐지 정규화기, 수집기, 수집 상태 판정, 한 화면 조회 API | T33 |
| [`services/control-runner`](../../services/control-runner/) | 통제 시험기. T34 감사 카나리 · 검색 연결과 T35 백업 · 복원 검증을 포함한다 | T14 · T34 · T35 |
| [`services/observability`](../../services/observability/) | 클러스터별 OTel Collector · 플랫폼 Prometheus 설정과 재현 시험(T37). 판정 기준 구현(T36)은 PR #137 | T36 · T37 |

## 5. 2026-11-04 00:04 KST 당시 열려 있던 PR

작성 당시의 목록을 보존한다. 현재 열림·병합 여부는 아래 PR 링크에서, 작업 완료 여부는 1절의 이슈와 프로젝트 보드에서 확인한다.

| PR | 작업 | 내용 |
|---|---|---|
| [#137](https://github.com/Softbank-Hackathon-Finals/platform/pull/137) | T36 | 판정 기준 구현 `bravo_observability`, 계약 시험을 CI에서 실행 |
| [#117](https://github.com/Softbank-Hackathon-Finals/platform/pull/117) · [#124](https://github.com/Softbank-Hackathon-Finals/platform/pull/124) | T34 | 환경 내부 OTel 감사 전달, OTLP 감사 원천 · 수신 시각 검증 |
| [#120](https://github.com/Softbank-Hackathon-Finals/platform/pull/120) · [#126](https://github.com/Softbank-Hackathon-Finals/platform/pull/126) · [#128](https://github.com/Softbank-Hackathon-Finals/platform/pull/128) | T35 | 백업 완료 결속, 상태 조회 최소권한, 잘못된 백업 · 인증서의 복원 거부 |
| [#122](https://github.com/Softbank-Hackathon-Finals/platform/pull/122) · [#127](https://github.com/Softbank-Hackathon-Finals/platform/pull/127) · [#132](https://github.com/Softbank-Hackathon-Finals/platform/pull/132) | T14 | DB · Pooler 네트워크 거부, kube-bench 결과, Pooler→DB TLS 판정 |

## 6. 설계 문서에 반영할 것 (제안)

설계 문서 담당과 협의해 반영하며, 의미 있는 변경이라 버전을 올린다([버전 규칙](../design-history/README.md)).

1. **11장 작업 목록에 T33~T37 추가.** 지금은 `tasks.md`와 이슈에만 있다.
2. **7.3 대응표에 "로그 · 모니터링" 줄 추가.** 출발지는 CloudWatch Logs · 지표, 목적지는 클러스터 로그 저장소 + OTel Collector, 검증은 AUD-01 · SEC-01.3 검색(ADR-0010).
3. **8.5에 통제 시험 계약 링크.** 하위 시험 20개, 게이트 검사 순서, 예외 승인 유효성은 `docs/controls/README.md`가 정본이라는 것만 적는다.
4. **2.1 킥오프 주제 해석.** "보안 · 거버넌스의 통합 관리"에 탐지 통합(T33)을, "비용 · 횡단 모니터링"에 관측(T36 · T37)을 대응시킨다.
