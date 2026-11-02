> **작성자:** 이상무 (보안 통제 · 게이트 담당)
> **원본:** 이 레포 (노션 사본이 아님). 작업 번호 T33.
> **상태:** 설계. 설계 문서(`design.html`) 스켈레톤 밖의 작업이라 이 문서에 따로 둔다. 형식 결정은 [ADR-0001](../adr/0001-security-finding-format.md).

# 보안 탐지 통합 (T33)

클라우드마다 다른 보안 탐지 기능(AWS GuardDuty · Security Hub, Google Cloud Security Command Center, 온프렘 Falco · Kyverno)의 결과를 **하나의 형식으로 정규화**해, 모든 환경의 권한 · 암호화 · 감사 로그 · 정책 위반 · 위협 탐지를 **같은 기준으로 한 화면**에 보인다. 정규화는 결정적 코드가 하고 LLM을 쓰지 않는다.

## 1. 왜 필요한가

킥오프 자료가 직접 요구한다.

| 킥오프 | 내용 |
|---|---|
| 16 · 17쪽 | "보안과 거버넌스를 모든 환경에서 같은 수준으로 지킬 수 있는가? **GuardDuty, Security Command Center 등 클라우드마다 다른 기능을 어떻게 통일하고 시각화**할 것인가?" |
| 18 · 19쪽 | 보안 · 거버넌스 통합: "클라우드마다 다른 보안 기능의 차이를 메움", "**권한 · 암호화 · 감사 로그 · 정책 위반을 한 화면에서 시각화**", "온프레미스에서도 같은 기준으로 보안을 시각화" |
| 20 · 21쪽 | 최종 데모에서 보여 줄 것: "**여러 환경의 보안 · 거버넌스 상태 시각화**" |
| 39 · 40쪽 | 클라우드 활용 30점: "여러 환경을 묶는 관리 기능(보안 · 거버넌스 등)이 적절히 **추상화 · 설계**되어 있는가" |

설계 문서의 6개 통제(T5)는 **이전 전후에 같은 시험을 돌려 설정을 비교**한다. 이 작업은 그와 다른 축이다. **각 클라우드의 탐지 서비스가 지금 무엇을 발견했는지**를 같은 형식으로 모은다. 두 축은 같은 화면에서 만난다(6절).

## 2. 범위

| 범위 안 | 범위 밖 |
|---|---|
| 출처별 탐지 결과 수집(읽기 전용) · 정규화 · 저장 · 조회 API | 탐지 엔진을 새로 만드는 것 (각 클라우드 · OSS 도구를 그대로 쓴다) |
| 환경별 탐지 능력(무엇을 볼 수 있는지)과 수집 상태 표시 | SIEM 전체 기능(상관 분석, 장기 보관 검색) |
| T5 통제 시험 결과를 같은 화면에 함께 표시 | 자동 대응 · 자동 수정 (수정은 에이전트 제안 → 리허설 → 승인 경로(T11 · T15)로만) |

## 3. 출처

**환경마다 집계 출처를 하나로 정한다.** 같은 탐지가 두 출처로 들어와 이중으로 세지는 것을 막는다(예: GuardDuty 탐지는 Security Hub에도 들어간다).

| 환경 | 집계 출처 (P0) | 대체 출처 | 원본 형식 | 담는 것 |
|---|---|---|---|---|
| AWS | **Security Hub** `GetFindings` | Security Hub를 켤 수 없으면 GuardDuty `ListFindings` · `GetFindings` 직접 | ASFF | GuardDuty(위협), 보안 표준 점검(설정 위반), Inspector(취약점, 켠 경우) |
| Google Cloud | **Security Command Center** `ListFindings` | — | SCC Finding | Security Health Analytics(설정 위반), 위협 탐지(등급에 따라) |
| 온프렘 (RKE2) | **Kyverno PolicyReport** (K8s API) + **Falco** (falcosidekick 웹훅) | — | PolicyReport CRD, Falco 이벤트 JSON | 정책 위반(Kyverno), 런타임 위협(Falco) |
| 모든 환경 | **T5 통제 시험 결과** (`ControlRun`) | — | `docs/controls/control-result.schema.json` | 6개 통제 PASS / FAIL / UNKNOWN |

- 서비스 이름과 API는 착수 시 실제 계정의 콘솔 · API에서 다시 확인한다(AWS · Google Cloud가 보안 서비스를 개편하고 있다).
- SCC는 등급(Standard / Premium 등)에 따라 제공되는 감지기가 다르다. 쓸 수 있는 감지기는 실제 프로젝트에서 확인하고 4절 "탐지 능력" 표에 그대로 적는다.
- Kyverno는 설계 문서에서 목적지 P1(T9)이다. Falco는 설계 문서에 없다. 둘 다 온프렘 설치 담당을 정해야 한다(9절).

## 4. 구조

```text
 [AWS]                [Google Cloud]          [온프렘 RKE2]
 Security Hub API     SCC API                 Kyverno PolicyReport   Falco ──(falcosidekick)
      │ 주기 조회           │ 주기 조회               │ 주기 조회            │ 웹훅 (온프렘 → 플랫폼, 바깥 방향)
      ▼                    ▼                       ▼                      ▼
 ┌──────────────────────────────── 수집기 (출처별 어댑터, 읽기 전용) ────────────────────────────────┐
 │  커서(마지막 갱신 시각) · 페이지 · 재시도 · 수집 상태 기록                                         │
 └───────────────────────────────────────────────┬──────────────────────────────────────────────────┘
                                                 ▼
                         정규화기 (결정적, LLM 미사용)  ── 원본은 마스킹 후 따로 보관
                                                 ▼
                         저장 (탐지 결과 · 수집 상태 · 탐지 능력)  ── 출처 + 원본 ID로 upsert
                                                 ▼
                         조회 API  ──▶  콘솔 T21 "한 화면"
```

- **수집기는 출처별 어댑터**다. 설계 문서 8.11의 환경 어댑터 방식(T16)과 같다. 새 클라우드(NAVER Cloud 등)는 어댑터 하나와 매핑 표 한 줄로 붙인다.
- **온프렘은 바깥 방향 연결만 쓴다**(설계 문서 7.5). Falco는 온프렘에서 플랫폼으로 보내고, Kyverno는 온프렘 쪽 수집 에이전트가 읽어 보낸다. 플랫폼이 온프렘으로 들어가는 포트를 열지 않는다.

## 5. 정규화 형식

[OCSF](https://schema.ocsf.io/) 1.1의 탐지 결과 클래스 부분집합에 플랫폼 확장 필드를 더한다. 왜 OCSF인지는 [ADR-0001](../adr/0001-security-finding-format.md).

**규칙의 원본은 데이터 파일이다.** 아래 표는 설명이고, 구현은 이 파일만 읽는다. 표와 파일이 다르면 파일이 맞다.

| 파일 | 내용 |
|---|---|
| [`detection/finding.schema.json`](detection/finding.schema.json) | 정규화 결과(`Finding`)와 출처 수집 상태(`SourceStatus`)의 JSON Schema |
| [`detection/mappings.json`](detection/mappings.json) | 출처별 심각도 · 상태 · 클래스 · 범주 규칙, 오래됨 기준 시간 |
| [`detection/examples/`](detection/examples/) | 출처마다 원본 발췌(`*.raw.json`, 형식 예시)와 그 정규화 결과(`*.normalized.json`), 수집 실패 상태 예시 |

| OCSF 클래스 | `class_uid` | 넣는 것 |
|---|---|---|
| Detection Finding | 2004 | 위협 탐지 (GuardDuty, SCC 위협, Falco) |
| Compliance Finding | 2003 | 설정 · 정책 위반 (보안 표준 점검, SCC 설정 위반, Kyverno, **T5 통제 시험**) |
| Vulnerability Finding | 2002 | 취약점 (Inspector, SCC 취약점) |

### 필드

| 필드 | 내용 |
|---|---|
| `class_uid`, `severity_id`, `status_id`, `time` | OCSF 공통. 심각도 · 상태는 아래 매핑 표로 정한다 |
| `finding_info.uid` | 출처의 원본 ID. `(source, uid)`가 upsert 키 |
| `finding_info.title`, `finding_info.types[]`, `first_seen_time`, `last_seen_time` | 원본에서 그대로 |
| `resources[]` | 대상 자원(유형 · ID · 이름). 계정 ID 등은 마스킹 규칙(7절) 적용 |
| `cloud.provider`, `cloud.region`, `cloud.account.uid` | 출처 환경 |
| `metadata.product.name`, `metadata.version` | 출처 제품(GuardDuty, SCC, Falco, Kyverno, bravo-controls)과 OCSF 버전 |
| `unmapped` | 매핑하지 못한 원본 필드 (버리지 않는다) |
| `bravo.environment_id` | 플랫폼에 등록된 환경 (설계 문서 8.11) |
| `bravo.category` | 한 화면의 범주: `IAM` 권한 · `ENCRYPTION` 암호화 · `AUDIT` 감사 로그 · `EXPOSURE` 네트워크 노출 · `POLICY` 정책 위반 · `THREAT` 위협 · `VULNERABILITY` 취약점 |
| `bravo.control_ids[]` | 관련 통제 ID (T5의 SEC-01 등). 없으면 빈 배열 |
| `bravo.native_url` | 원본 콘솔에서 보는 링크 |
| `bravo.raw_sha256` | 마스킹한 원본의 해시 (원본 대조용) |

### 심각도 매핑 (`severity_id`)

OCSF: 1 Informational · 2 Low · 3 Medium · 4 High · 5 Critical, 판단할 수 없으면 0 Unknown.

| 출처 | 원본 값 → `severity_id` |
|---|---|
| Security Hub (ASFF `Severity.Label`) | INFORMATIONAL 1 · LOW 2 · MEDIUM 3 · HIGH 4 · CRITICAL 5 |
| GuardDuty 직접 (숫자 `Severity`) | 등급 경계는 착수 시 공식 문서로 확인해 고정한다. 확정 전에는 Security Hub 경유만 쓴다 |
| SCC (`severity`) | LOW 2 · MEDIUM 3 · HIGH 4 · CRITICAL 5 · SEVERITY_UNSPECIFIED 0 |
| Falco (`priority`) | DEBUG · INFORMATIONAL 1 · NOTICE 2 · WARNING 3 · ERROR 4 · CRITICAL · ALERT · EMERGENCY 5 |
| Kyverno (정책 annotation `policies.kyverno.io/severity`) | low 2 · medium 3 · high 4 · critical 5 · 없음 3 |
| T5 통제 시험 | FAIL 4 · UNKNOWN 3 (확인 불가는 차단 사유라 낮추지 않는다) |

### 상태 매핑 (`status_id`)

OCSF: 1 New · 2 In Progress · 3 Suppressed · 4 Resolved.

| 출처 | 원본 → `status_id` |
|---|---|
| Security Hub | `RecordState` ARCHIVED → 4. 아니면 `Workflow.Status`: NEW 1 · NOTIFIED 2 · SUPPRESSED 3 · RESOLVED 4 |
| SCC | `state` INACTIVE → 4. ACTIVE이면서 `mute` MUTED → 3, 아니면 1 |
| Falco | 이벤트라 상태가 없다 → 1. 같은 규칙 · 같은 대상이 정해진 시간(기본 24시간) 다시 안 나오면 4 |
| Kyverno | 결과 `fail` · `warn` → 1(유지되면 그대로), 같은 정책 · 대상이 `pass`로 바뀌면 4. `skip`은 버리고, `error`는 수집 문제로 본다(7절) |
| T5 | FAIL · UNKNOWN → 1, 다음 실행에서 PASS → 4 |

### 범주 매핑 (`bravo.category`)

출처 고유 분류를 규칙 표로 범주에 넣는다. 아래는 첫 규칙이고, 매핑되지 않은 분류는 `POLICY`(설정 위반) 또는 `THREAT`(위협)로 넣고 `unmapped`에 원본 분류를 남긴다.

| 출처 | 원본 분류 (예) | 범주 |
|---|---|---|
| ASFF `Compliance.SecurityControlId` | `IAM.`으로 시작 | `IAM` |
| ASFF `Compliance.SecurityControlId` | `KMS.`으로 시작 | `ENCRYPTION` |
| ASFF `Compliance.SecurityControlId` | `CloudTrail.`으로 시작 | `AUDIT` |
| ASFF `Compliance.SecurityControlId` | S3 · EC2 등 서비스 안에서 노출 · 암호화가 섞인 점검 | 점검 ID 단위로 실제 결과를 보고 추가. 그 전에는 `POLICY` |
| ASFF `Types` | `TTPs/...`, `Unusual Behaviors/...`, `Effects/...` (GuardDuty) | 클래스 2004 → `THREAT` |
| ASFF `Types` | `Software and Configuration Checks/Vulnerabilities/...` | 클래스 2002 → `VULNERABILITY` |
| SCC `findingClass` | THREAT → `THREAT`, VULNERABILITY → `VULNERABILITY`, MISCONFIGURATION → `category`로 세분 (예: PUBLIC_BUCKET_ACL → `EXPOSURE`) | |
| Falco | 모든 규칙 | `THREAT` |
| Kyverno | 정책별로 정한다 (예: 비루트 강제 → `POLICY`, 시크릿 평문 env 금지 → `ENCRYPTION`) | |
| T5 | SEC-01 · ENC-02 → `ENCRYPTION`, ENC-01 → `ENCRYPTION`, AUD-01 → `AUDIT`, NET-01 → `EXPOSURE`, IAM-01 · ISO-01 → `IAM`, DR-01 → `POLICY` | |

매핑 표는 코드가 아니라 데이터(설정 파일)로 둔다. 바꿀 때는 표와 단위 테스트를 함께 고친다.

## 6. 한 화면 (콘솔 T21에 주는 것)

| 조회 API | 돌려주는 것 |
|---|---|
| `GET /v1/security/summary` | **환경 × 범주** 칸마다 심각도별 미해결 건수와 그 칸의 수집 상태 |
| `GET /v1/security/findings?env=&category=&severity=&status=` | 정규화된 탐지 결과 목록 (원본 링크 포함) |
| `GET /v1/security/coverage` | **환경 × 탐지 능력** 표 (아래) |

```text
                 IAM   암호화   감사   노출   정책   위협   취약점
AWS (출발지)      2H    0       0      1C     3M     1H     5M
온프렘 (목적지)   0     1H      확인불가 0     2M     0      확인불가
Google Cloud     …
```

**탐지 능력 표**는 "같은 기준으로 지키는가"에 대한 답이다. 환경마다 어떤 탐지 능력이 있는지 보인다.

| 탐지 능력 | AWS | 온프렘 | Google Cloud |
|---|---|---|---|
| 런타임 위협 (컨테이너 · 호스트) | GuardDuty (EKS 보호를 켠 경우) | Falco | SCC 위협 탐지 (등급에 따라) |
| 네트워크 · API 위협 | GuardDuty | 없음 → 화면에 "없음"으로 표시 | SCC 위협 탐지 (등급에 따라) |
| 설정 · 정책 위반 | 보안 표준 점검 | Kyverno + T5 | Security Health Analytics |
| 취약점 | Inspector (켠 경우) | 없음 (P1: Trivy Operator) | SCC 취약점 |
| 이전 전후 통제 비교 | T5 | T5 | T5 |

"없음"을 숨기지 않는다. 목적지에 출발지만큼의 탐지 능력이 없으면 그것 자체가 이전 위험이다. 게이트(T19)에 넣을지는 스켈레톤 밖이라 지금은 화면에만 보이고, 넣자는 제안은 따로 한다.

## 7. 프로덕션 요건

**수집 실패는 "0건"이 아니다.** T5의 UNKNOWN 원칙과 같다.

| 상황 | 화면 표시 |
|---|---|
| 마지막 수집이 성공했고 기준 시간(기본 15분) 안 | 건수 그대로 |
| 수집 실패 (권한 없음, API 오류, 웹훅 끊김) | 그 칸 **"확인 불가"** + 마지막 성공 시각 |
| 기준 시간이 지남 | 그 칸 **"오래됨"** + 마지막 성공 시각 |
| 출처 서비스가 꺼져 있음 (예: Security Hub 미활성) | 탐지 능력 표에 "없음" |

| 영역 | 요건 |
|---|---|
| 권한 | 출처마다 읽기 전용 최소 권한. AWS `securityhub:GetFindings`(대체 경로는 `guardduty:ListDetectors` · `ListFindings` · `GetFindings`), Google Cloud SCC 결과 조회 전용 역할, K8s는 `policyreports` · `clusterpolicyreports`의 `get` · `list`만. 플랫폼 장기 키를 두지 않는다(AWS는 역할 위임, Google Cloud는 Workload Identity 연동) |
| 멱등 · 중복 | `(source, finding_info.uid)`로 upsert. 같은 탐지의 갱신은 덮어쓰고 `last_seen_time`만 늘린다. 환경마다 집계 출처가 하나라 출처 간 중복이 없다 |
| 증분 수집 | 출처별 커서(마지막 갱신 시각)를 저장하고 그 이후만 가져온다. 페이지를 끝까지 돈다. 커서는 한 번의 수집이 끝까지 성공했을 때만 전진 |
| 재시도 | 출처 API의 속도 제한 응답에는 지수 백오프. 실패는 수집 상태에 기록하고 화면에 보인다 |
| 원본 보관 | 원본은 마스킹한 뒤 별도 테이블에 보관하고 `raw_sha256`으로 잇는다. 접근 키 ID · 토큰 · IP는 일부만 남긴다. 비밀 원문은 저장하지 않는다 |
| 웹훅 인증 | Falco(falcosidekick) → 플랫폼은 Tailscale 경로 + 공유 토큰(회전 가능). 토큰 없는 요청은 401, 수신은 로그에 남긴다 |
| 결정적 처리 | 정규화 · 범주 · 심각도는 규칙 표로만 정한다. LLM은 쓰지 않는다. 요약 · 설명이 필요하면 에이전트 제안 경로(T11)로 따로 붙인다 |
| 시간 | 모든 시각은 UTC. 출처 시각과 수집 시각을 따로 저장한다 |

## 8. 데모에서 실제 데이터로 보이는 방법

목업이 아니라 실제 탐지 결과로 보인다. 반영 지연은 출처마다 다르므로 실측해서 데모 각본(T28)에 반영한다.

| 환경 | 실제 탐지를 만드는 방법 |
|---|---|
| AWS | GuardDuty `CreateSampleFindings` API로 샘플 탐지 생성(결과에 샘플 표시가 붙는다) → Security Hub 경유 수집. 시험용 버킷을 공개로 바꿔 보안 표준 점검 위반 생성 |
| 온프렘 | 앱 파드에 `kubectl exec`로 셸 실행 → Falco 기본 규칙(컨테이너 안 터미널 셸) 탐지. 비루트 정책을 어기는 파드 배포 → Kyverno PolicyReport `fail` |
| Google Cloud | 시험용 버킷 공개 → Security Health Analytics 탐지 (등급에 포함된 경우) |
| 수집 실패 표시 | Falco 웹훅 토큰을 바꿔 수집을 끊음 → 온프렘 "위협" 칸이 "확인 불가"로 바뀜 |

## 9. 열린 질문

| 질문 | 누구와 |
|---|---|
| 플랫폼 구현 언어 · 저장소(DB) · API 위치 | T10 담당(류지환) 결정을 따른다 |
| AWS Security Hub · GuardDuty 활성화와 비용(각각 무료 체험 기간 후 과금) | T1 담당(류지환) |
| Google Cloud 프로젝트와 SCC 등급 | 계정 담당(류지환) |
| 온프렘 Falco 설치, Kyverno 설치 시점(T9는 P1) | T2 · T30 담당(김태현), T9 담당(전동훈) |
| 탐지 능력 동등성을 게이트(T19) 질문으로 넣을지 | 팀 논의 (스켈레톤 변경이라 설계 문서 버전을 올려야 함) |
