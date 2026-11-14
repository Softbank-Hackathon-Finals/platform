# [ADR-0034] 환경 종류 어댑터와 중립 모델: aws · gcp · onprem 3×3 이전

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** A1~A11, 설계 문서 8.11(환경 어댑터), [ADR-0018](0018-dependency-grading-rules.md), [ADR-0021](0021-approved-change-promotion.md), [ADR-0032](0032-destination-iac-api.md), ADR-0033(#621), 계약 [environment-kinds.json](../contracts/environment-kinds.json) · [neutral-models.schema.json](../contracts/neutral-models.schema.json)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 설계 8.11은 "출발지 · 목적지 종류 무관, 환경 차이는 어댑터로 분리"를 전제로 한다. 구현은 AWS 출발지 → 온프렘 목적지 한 쌍뿐이다. develop `bd9f3aa8` 기준으로 상태 머신(`PlanSpec`), 게이트(`control-gate/engine.py`), Executor(`sites.py`의 origin 기준 방향 판정), Edge(Cloudflare pool 두 개), tailnet 모듈, Argo CD 차트는 이미 종류를 모른다. 되돌리기는 같은 Executor를 반대 방향으로 실행해 k3d에서 DONE까지 갔다(ADR-0016).
* **문제:**
  * 등록이 `kind: Literal["aws","onprem"]`이고 aws는 출발지만 될 수 있다(`registration_api.py:27`, `registration_probe.py:40-52`).
  * 번역이 쌍마다 직접 구현돼 있다. S3 정책 → SeaweedFS 정책(`rehearsal-runner/bucket_proposer.py`), Secrets Manager → OpenBao(`secret_task.py`), SeaweedFS `s3.json` 고정 반영(`apply-path/policy.py`). 이대로 9개 조합을 만들면 번역기가 조합 수만큼 늘어난다.
  * 출발지 분석은 AWS(boto3 · IRSA · ARN), 목적지는 온프렘(카탈로그 · 리허설 · 구축 스택)만 안다. 온프렘 앱의 `boto3` + `endpoint_url`(SeaweedFS)을 AWS 의존성으로 오판하고, aws→aws 출구 시험은 `*.amazonaws.com`을 막아 목적지까지 막는다.
* **목표:** aws · gcp · onprem을 출발지와 목적지로 자유롭게 고르는 9개 조합. 같은 종류 간 이전은 "다른 클러스터로의 이전"이다. 운영팀이 손으로 하던 번역 · 권한 · 구축을 플랫폼이 대신해 이전의 자유를 준다.

## 2. 고려한 옵션들 (Considered Options)

### 1. 조합별 구현

조합마다 수집 · 번역 · 리허설 · 반영 코드를 따로 둔다.

**Pros**
* 조합 하나만 보면 가장 빨리 만든다.

**Cons**
* 9개 조합에 9벌, 종류가 하나 늘면 2N+1벌이 더 필요하다.
* 같은 통제를 조합마다 다르게 시험할 위험이 있다.

### 2. 종류별 어댑터 + 중립 모델(허브)

어댑터를 출발지 쪽 · 목적지 쪽 · 양쪽 공통 칸으로 나누고 종류마다 한 벌씩 만든다. 번역은 `출발지 → 중립 모델 → 목적지`로만 한다.

**Pros**
* 작업량이 조합 수가 아니라 종류 수에 비례한다(3종류 × 10칸).
* 에이전트는 중립 모델과 코드 diff만 제안하고, 목적지 정책은 결정적 코드가 렌더한다.
* 기존 공통부(상태 머신 · 게이트 · Executor · Edge)를 그대로 쓴다.

**Cons**
* 중립 모델로 표현하지 못하는 정책(조건부 IAM 등)은 `unresolved`로 남겨 사람 확인이 필요하다.

## 3. 결정 사항 (Decision Outcome)

* **옵션 2를 선택한다.**
* **관리형 DB(RDS · Cloud SQL) 출발지와 한 배포에서 여러 쌍을 동시에 운영하는 것은 향후 확장으로 남긴다.**
* **조합별 직접 번역은 새로 만들지 않는다.** 기존 S3 → SeaweedFS 번역은 S3 내보내기 + SeaweedFS 적용으로 나눈다.

### 3.1 어댑터 칸

칸 이름과 종류 목록의 원본은 [environment-kinds.json](../contracts/environment-kinds.json)이다. 서비스는 이 키로 자기 등록부를 만든다.

| 칸 | 하는 일 | 주 서비스 |
|---|---|---|
| `source.static_rules` | SDK · IaC · endpoint → 역할. 출발지 종류가 S3 호환 호출의 의미를 정한다 | inventory |
| `source.deploy_trace` | 신원 → 정책 → 자원 추적(`DeployAdapter`) | inventory |
| `source.neutral_export` | 출발지 정책 → 중립 모델 | rehearsal-runner |
| `source.exit_scope` | 출구 시험 차단 대상. 같은 종류 목적지는 계정 · 프로젝트 · 클러스터 범위로 구분 | exit-drill |
| `target.catalog` | 역할 → 구현 · 등급 · 검증 · 제공 기능 | inventory, target-recommend |
| `target.neutral_apply` | 중립 모델 → 목적지 객체 노출 · 비밀 전달 | rehearsal-runner, apply-path, iac |
| `target.rehearsal` | 격리 프로필 · 자격 Job · 버킷 Job | rehearsal-runner |
| `target.build` | 공통 기반을 갖춘 목적지 구축 스택 | infra/stacks, infra/iac |
| `both.control_probes` | `(kind, test_id) → Probe`. ENC-01 · NET-01 · DR-01은 종류 무관 | control-runner |
| `both.identity` | 등록 시 읽기 전용 신원 · 연결 확인 | platform-events |

### 3.2 규칙

1. **모든 종류가 출발지와 목적지가 될 수 있다.** 역할은 종류에서 추론하지 않고 등록 때 명시한다.
2. **같은 종류 간 이전은 클러스터 신원과 DB 신원이 모두 달라야 한다.** 출구 시험은 호스트 접미사가 아니라 `scope_keys`(aws: 계정 · 리전, gcp: 프로젝트 · 리전, onprem: 클러스터 UID)로 출발지만 막는다.
3. **공통 기반:** 모든 환경은 Kubernetes + CNPG + PgBouncer + Tailscale operator + cloudflared를 갖춘다. 데이터 경로와 Executor는 이 전제로 공통부로 남는다. EKS · GKE 목적지도 `target.build`가 CNPG를 설치한다.
4. **중립 모델:** `ObjectAccessPolicy`, `SecretBinding` 두 개로 시작한다([스키마](../contracts/neutral-models.schema.json)). 값 · 자격은 담지 않는다. 내보내기가 주체를 워크로드로 매핑하지 못하면 `unresolved`로 남기고 적용기는 그 권한을 주지 않는다.
5. **없는 칸은 차단한다.** 어댑터가 없으면 `UNSUPPORTED`를 돌려주고 게이트는 기존 규칙대로 UNKNOWN = 차단으로 처리한다. 계획 등록은 조합 준비도를 계산해 `adapter_missing:<칸>`으로 거부한다.
6. **카탈로그 등급:** 목적지 기본 등급 + `(출발지, 목적지, 역할)` 예외표. 같은 종류 간 이전의 기본은 `config_swap`이다.
7. **플랫폼 자체의 AWS 의존(감사 S3 Object Lock, 운영자 SSO, ECR)은 이 결정의 범위가 아니다.**

### 3.3 작업 번호

이 결정의 구현은 `A번호`로 관리한다. PR 제목과 커밋은 `[A번호]`로 시작하고 브랜치는 `feature/A번호-설명`이다.

| 번호 | 내용 |
|---|---|
| A1 | 이 ADR, 종류 계약, 중립 모델 스키마, `[A번호]` 제목 허용 |
| A2 | 등록 · 콘솔 · 관측: gcp 추가, 종류별 역할 고정 해제, 역할 라벨 기준 경보 |
| A3 | 출발지 분석: 종류별 정적 탐지 규칙, GKE Workload Identity · 온프렘 추적 |
| A4 | 목적지 카탈로그 허브: aws 목적지, gcp 보완, 조합 예외표, 추천 |
| A5 | 리허설 중립 번역: 내보내기 · 적용 분리, 목적지 종류별 리허설 실행부 |
| A6 | 반영 중립 적용: apply-path 목적지 분기, IaC 객체 노출 모듈 |
| A7 | 통제 시험 등록부: 종류별 Probe, GCP 시험, 온프렘 빈칸 보완 |
| A8 | 출구 시험 범위: 종류별 차단 대상, 같은 종류 범위 구분 |
| A9 | Executor 이름 일반화와 9개 조합 모의 시험 |
| A10 | 목적지 구축 스택: EKS · GKE, GCP 기반 Terraform |
| A11 | 실환경 검증 |

## 4. 결과

* 9개 조합 중 어댑터가 모두 채워진 조합만 계획 등록을 통과한다. 나머지는 이유와 함께 거부된다.
* 새 종류를 추가할 때는 계약에 종류를 더하고 10칸을 채운다. 공통부는 바꾸지 않는다.
* 다른 담당자의 서비스(T10 등록, T15 리허설, T19 게이트, T44 IaC, T12 Executor)를 바꾸는 A 작업은 PR에서 해당 담당자 리뷰를 받는다.
