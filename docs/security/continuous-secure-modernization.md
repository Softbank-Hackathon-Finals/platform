> **작성자:** 이상무 (보안 통제 · 게이트 담당)
> **원본:** [노션 (팀원 전용)](https://app.notion.com/p/Patching-as-a-Service-40c8bee9ada4804ca2b9d33928e33c98). 이 문서는 2026-11-03 기준 사본이다. 고칠 때는 노션을 먼저 고친다.
> **상태:** 보안 control catalog와 플랫폼 실행 게이트가 구현됐다. 실환경 서비스·근거 생산자 인수는 별도다.
> **출처 확인:** SoftBank 발표 내용과 인용 자료는 발표 · 설계 문서 반영 전에 원문을 확인한다(T23).
> **플랫폼 적용 계약:** [Platform security governance enforcement](platform-security-governance.md)

# Patching as a Service 기반 지속적 보안 개선 및 멀티클라우드 마이그레이션 아키텍처

**문서 유형:** Architecture Extension / Security Modernization  
**작성일:** 2026-11-02  
**상태:** 설계 보완안  
**관련 문서:** [실제 사이버공격 사례 기반 멀티클라우드 보안 아키텍처 설계](incident-based-architecture.md)  
**적용 대상:** 프로덕션급 멀티클라우드 관리·마이그레이션 플랫폼

---

# 1. 개요

## 1.1. 설계 배경

SoftBank는 2026년 *Patching as a Service*를 발표하며 AI를 활용한 취약점 진단, 공격 가능성 평가, 수정안 생성, 검증 및 패치 적용을 지원하는 방향을 제시했다.

해당 접근방식은 취약점을 발견하는 것에서 끝나지 않고, 발견된 문제를 안전하게 수정하고 지속적으로 재평가하는 운영 체계를 지향한다.

기존 멀티클라우드 관리·마이그레이션 아키텍처는 운영 중인 시스템의 의존성 조사, 이전 계획, 격리 리허설, 승인, 단계적 이전 및 검증을 중심으로 설계되어 있다.

두 접근방식은 상호 보완적이다.

- Patching as a Service는 운영 시스템의 취약점을 지속적으로 발견하고 개선하는 과정을 강조한다.
- 멀티클라우드 마이그레이션은 운영 시스템을 다른 환경으로 이전하면서 기능·데이터·보안·가용성을 보존하는 과정을 강조한다.

따라서 취약점 수정과 마이그레이션을 별도의 기능으로 구현하기보다, 동일한 정책·승인·검증·실행 체계를 사용하는 변경 작업으로 통합한다.

## 1.2. 핵심 설계 목표

**AI-Assisted Patching + Security-Preserving Migration + Continuous Verification**

AI 기반 보안 개선과 안전한 운영 환경 이전을 통합하여, 클라우드 종속성을 낮추면서 기존 보호 수준과 운영 연속성을 유지하는 것이 목표다.

이를 위해 다음을 보장하도록 설계한다.

1. 운영 시스템의 취약점과 보안 설정 문제를 지속적으로 조사한다.
2. 실제 공격 가능성과 운영 영향을 고려해 수정 우선순위를 결정한다.
3. AI가 코드·설정·IaC 수정안을 생성한다.
4. 변경사항을 격리 환경에서 검증한다.
5. 정책 검증과 승인 이후에만 운영 환경에 적용한다.
6. 패치와 마이그레이션에 동일한 실행 통제를 적용한다.
7. 이전 전후의 보안 보호 수준을 비교한다.
8. 변경 결과와 복구 가능성을 실제 운영 상태로 확인한다.
9. 새로운 취약점 및 보안 설정 드리프트가 발생하면 재평가한다.

## 1.3. 기존 아키텍처와의 관계

본 설계는 기존 멀티클라우드 관리 아키텍처를 대체하지 않는다.

기존의 실행 계약, 정책 엔진, 격리 샌드박스, 승인 게이트, 클라우드 어댑터, 상태 원장 및 복구 체계를 유지하면서 지속적인 보안 개선 기능을 추가한다.

**기존 흐름**

```text
시스템 등록
    ↓
의존성 조사
    ↓
이전 계획
    ↓
격리 리허설
    ↓
승인
    ↓
단계적 이전
    ↓
기능·데이터·보안 검증
    ↓
지속 관측
```

**보완된 흐름**

```text
시스템 등록 및 지속 조사
    ↓
취약점·보안 설정·의존성 분석
    ↓
공격 위험·이전 가능성 평가
    ↓
AI 수정안 및 이전 계획 생성
    ↓
격리된 패치·이전 리허설
    ↓
보안·기능·데이터 검증
    ↓
정책 검증 및 승인
    ↓
패치 적용 또는 단계적 이전
    ↓
실제 운영 상태 검증
    ↓
지속적인 재진단 및 개선
```

---

# 2. SoftBank Patching as a Service 분석

## 2.1. 개념

SoftBank가 발표한 Patching as a Service는 AI를 활용해 소프트웨어와 운영 환경의 취약점을 진단하고, 수정안을 제안·검증하여 패치 적용까지 지원하는 서비스다.

공식 발표에서 제시한 주요 기능은 다음과 같이 정리할 수 있다.

| 단계 | 기능 | 내용 |
|---|---|---|
| 1 | Source Code Assessment | 소스 코드의 취약점 및 문제 분석 |
| 2 | Attack Assessment | 공격자 관점의 취약점 악용 가능성 평가 |
| 3 | Remediation Planning | 위험 평가 및 수정안·우선순위 결정 |
| 4 | Patch Deployment | 검증된 패치의 적용 지원 |

이 과정은 일회성 수정뿐 아니라 지속적인 진단과 개선을 목표로 한다.

## 2.2. 핵심 설계 원칙

### AI 기반 취약점 개선

AI는 소스 코드와 실행 환경을 조사하고 취약점을 해결하기 위한 수정안을 제안한다.

### 실제 검증 중심

AI가 수정안을 생성했다는 사실만으로 취약점이 해결됐다고 판단하지 않는다.

코드와 설정을 실제로 변경한 후 보안 검사 및 기능 검증을 수행해야 한다.

### 고객 변경관리 절차 존중

운영 시스템에 대한 변경은 독립적인 정책 검증과 고객 승인 절차를 통과해야 한다.

### 지속적인 개선

새로운 취약점, 라이브러리 변경, 인프라 설정 변경이 발생하면 기존 시스템을 다시 평가한다.

## 2.3. 멀티클라우드 설계와의 차이

| 비교 항목 | Patching as a Service | 멀티클라우드 관리·마이그레이션 |
|---|---|---|
| 중심 목표 | 취약점 개선 | 운영 환경 종속성 해소 및 안전한 이전 |
| 분석 대상 | 코드·취약점·공격 가능성 | 코드·인프라·의존성·보안·데이터 |
| AI 활용 | 보안 수정안 생성 | 코드·인프라 변경 및 이전 계획 |
| 사전 검증 | 패치 및 회귀 테스트 | 기능·데이터·보안·이전 리허설 |
| 변경 통제 | 고객 변경관리 | 정책·승인·실행 계약 |
| 실제 실행 | 패치 적용 | 패치 적용 및 단계적 이전 |
| 사후 검증 | 취약점 재진단 | 보안·기능·데이터 정합성·복구 검증 |

## 2.4. 통합 방향

Patching as a Service의 핵심 접근방식을 멀티클라우드 운영에 적용하되, 동일 서비스를 그대로 재현하는 것이 목표는 아니다.

취약점 수정 과정을 마이그레이션의 준비·실행·검증 과정에 통합하여, 운영 환경을 이전하면서 기존 보호 요구를 충족하고 필요한 보안 개선도 함께 수행한다.

---

# 3. Continuous Secure Modernization

## 3.1. 정의

**Continuous Secure Modernization은 운영 시스템의 취약점과 환경 종속성을 지속적으로 조사하고, AI가 제안한 수정과 이전 계획을 검증·승인·적용한 뒤 결과를 재평가하는 운영 체계다.**

## 3.2. 전체 실행 흐름

```text
Continuous Discovery
    │
    ▼
Risk & Portability Assessment
    │
    ▼
AI-Assisted Remediation
    │
    ▼
Isolated Rehearsal
    │
    ▼
Security & Functional Validation
    │
    ▼
Policy Gate & Human Approval
    │
    ▼
Progressive Deployment / Migration
    │
    ▼
Operational Verification
    │
    ▼
Continuous Monitoring & Revalidation
    │
    └──── 변경·취약점 발견 시 재평가
```

## 3.3. 단계별 역할

| 단계 | 주요 작업 | 검증 기준 |
|---|---|---|
| Continuous Discovery | 자산·코드·의존성·취약점 조사 | 조사 범위와 증거 확보 |
| Risk Assessment | 악용 가능성·영향·노출 분석 | 위험 평가 근거 |
| Portability Assessment | 환경 종속성·대상 호환성 확인 | 이식 가능성 분석 |
| AI Remediation | 코드·설정·IaC 변경안 제안 | 구조화된 수정안 및 diff |
| Isolated Rehearsal | 격리 환경에서 수정·빌드·실행 | 샌드박스 검증 |
| Security Validation | 취약점 해소·보호 수준 확인 | 재진단 결과 |
| Functional Validation | 기능·성능·데이터 호환성 검사 | 회귀 테스트 |
| Policy & Approval | 정책 검사·승인 계약 확정 | 승인 해시·조건 검증 |
| Deployment / Migration | 패치 또는 환경 이전 실행 | 실제 실행 상태 |
| Continuous Revalidation | 변경 이후 보안 상태 재조사 | 드리프트·재발 탐지 |

---

# 4. 추가 아키텍처 구성 요소

## 4.1. 구성 요소 목록

| 구성 요소 | 책임 | 기존 보안 아키텍처 연계 |
|---|---|---|
| Vulnerability Intelligence | 취약점·위협 정보 수집 | 공급망 및 취약점 대응 |
| Attack Surface & Exposure Analyzer | 외부 노출·권한·네트워크 경로 평가 | XSS·RCE·측면 이동 |
| AI Remediation Planner | 코드·설정·IaC 수정안 생성 | AI 권한 통제 |
| Patch Rehearsal Engine | 격리 패치 검증 | Sandbox·Policy Gate |
| Change Impact Analyzer | 기능·데이터·환경 호환성 평가 | 무중단·정합성 |
| Continuous Revalidation | 변경 이후 재진단 | 감사·관측·정책 엔진 |

---

# 5. Vulnerability Intelligence

## 5.1. 목적

취약점을 단순 CVSS 점수만으로 우선순위화하지 않고 실제 운영 환경의 노출 정도와 사업 영향을 고려한다.

## 5.2. 수집 정보

- CVE 및 취약점 권고
- CISA Known Exploited Vulnerabilities(KEV)
- 취약한 패키지와 실제 설치 버전
- 컨테이너 이미지 및 SBOM
- 인터넷 노출 여부
- 해당 코드의 실행 및 접근 가능성
- 자산 중요도
- 관련 데이터 분류
- 취약점 악용에 필요한 사전 권한
- 패치 호환성
- 변경에 따른 운영 위험
- 대상 클라우드에서의 보안 통제 가능 여부

## 5.3. 위험 평가

CVSS는 참고 지표로 활용하되 실제 악용 여부, 네트워크 노출, 서비스 중요도, 데이터 민감도 및 패치 위험을 함께 평가한다.

예를 들어 동일한 CVSS 9.8 취약점이라도 인터넷에 공개된 운영 시스템과 외부 통신이 차단된 개발 시스템은 대응 우선순위가 다를 수 있다.

## 5.4. 출력

```text
Asset
    ↓
Installed Components
    ↓
Known Vulnerabilities
    ↓
Exploit Evidence & Exposure
    ↓
Business Impact
    ↓
Remediation Priority
```

위험 평가 결과에는 판단에 사용한 취약점 정보와 실제 자산 증거를 연결한다.

---

# 6. AI Remediation Planner

## 6.1. 역할

AI가 취약점을 진단하는 데서 그치지 않고 코드·설정·IaC 변경안을 생성하도록 한다.

단, AI는 운영 권한 주체가 아니며 실제 실행은 별도 정책 및 승인 체계에서 통제한다.

## 6.2. 수정 범위

| 발견된 문제 | 수정 제안 | 독립 검증 |
|---|---|---|
| 오래된 인증 라이브러리 | 인증 구성 업데이트 | 인증·인가 회귀 테스트 |
| 취약한 npm 패키지 | 안전한 버전으로 업데이트 | SBOM·빌드·기능 검사 |
| 과도한 IAM 권한 | 권한 범위 축소 | IAM 정책·접근 검증 |
| 공개 RDS | 비공개 접근 구성 | 네트워크·DB 연결 검사 |
| AWS 종속 코드 | 대상 환경 대응 기능으로 변경 | GCP·온프레미스 기능 검사 |
| 컨테이너 루트 실행 | 비루트 실행 구성 | 런타임 검증 |
| SQL Injection | 파라미터화 쿼리 적용 | 보안·회귀 테스트 |
| TLS 인증서 검증 미흡 | 신뢰된 CA와 검증 설정 적용 | 실제 TLS 연결 검사 |

## 6.3. AI 실행 권한 제한

AI는 다음을 수행할 수 있다.

- 승인된 소스 조회
- 취약점·의존성 분석
- 수정안 및 코드 diff 생성
- 격리 작업 공간의 파일 수정
- 제한된 빌드·테스트 호출
- 변경 결과 분석
- 구조화된 실행 계획 제안

AI는 다음을 직접 수행할 수 없다.

- 운영 클라우드 권한 발급
- 운영 DB 원문 비밀 조회
- 승인되지 않은 리소스 삭제
- 임의의 운영 계정 선택
- 감사 기록 수정
- 승인 정책 변경
- 승인되지 않은 운영 배포

---

# 7. Patch Rehearsal Engine

## 7.1. 목적

AI 수정안이 실제 취약점을 해결하고 기존 서비스의 기능과 마이그레이션 호환성을 유지하는지 운영 환경 적용 전에 검증한다.

## 7.2. 실행 절차

```text
취약점 발견
    ↓
원본 소스·설정 스냅샷
    ↓
AI 수정안 생성
    ↓
격리 작업 공간
    ↓
변경 diff 검증
    ↓
빌드 및 테스트
    ↓
보안 재검사
    ↓
기능 회귀 검사
    ↓
원본 환경 호환성 검사
    ↓
목적지 환경 호환성 검사
    ↓
Patch Candidate 확정
```

## 7.3. 검증 증거

각 수정안에 다음을 연결한다.

| 증거 | 내용 |
|---|---|
| Original Finding | 수정 전 취약점 및 설정 문제 |
| Source Snapshot | 수정 전 코드·설정 해시 |
| Patch Diff | 변경된 코드·설정 |
| Build Evidence | 빌드 결과와 이미지 digest |
| Security Test | 수정 후 취약점 검사 |
| Regression Test | 기존 기능 영향 |
| Compatibility Test | 원본 및 목적지 실행 환경 |
| Policy Result | 보안 정책 허용·거부 |
| Approval | 승인자와 승인 계획 해시 |
| Deployment Evidence | 운영 적용 결과 |
| Recovery Evidence | 실패 시 복구 검증 |

## 7.4. 완료 판정

다음 조건이 충족되어야 패치 적용 후보로 승인할 수 있다.

- 원래 취약점이 해결된 것으로 검증됨
- 필수 기능 회귀 테스트 통과
- 기존 데이터 정합성 유지
- 필요한 보안 속성 유지
- 실행 환경과의 호환성 통과
- 추가된 위험이 허용 기준 이내
- 변경 내용과 실행 대상이 기록됨

검증 실패 또는 결과 불확실성은 성공으로 처리하지 않는다.

---

# 8. Change Impact Analyzer

## 8.1. 목적

보안 패치가 시스템의 기존 기능과 멀티클라우드 이전 계획에 미치는 영향을 평가한다.

## 8.2. 분석 대상

- 패키지 버전 변경
- DB 드라이버 변경
- TLS 정책 변경
- IAM 역할 및 권한 변경
- 네트워크 접근 정책 변경
- 컨테이너 런타임 변경
- API 호환성
- 데이터 스키마
- 비밀정보 참조 방식
- 클라우드 종속 서비스 교체

## 8.3. 대표 사례: PostgreSQL 드라이버 패치

```text
PostgreSQL 드라이버 보안 업데이트
    ↓
강화된 TLS 검증 요구
    ↓
기존 AWS RDS 연결 정상
    ↓
온프레미스 PostgreSQL 연결 실패
    ↓
목적지 인증서 구성 문제 탐지
    ↓
CA·서버 신원 검증 설정 수정
    ↓
원본·목적지 재검증
    ↓
이전 가능 판정
```

이 사례에서는 보안 패치가 적용되었다는 사실만으로 변경을 완료하지 않는다.

다음 세 조건이 충족되어야 한다.

1. 보안 취약점 개선 성공
2. 원본 환경의 정상 동작
3. 목적지 환경의 기능·보안 호환성 통과

---

# 9. Continuous Revalidation

## 9.1. 목적

운영 환경의 패치 적용 이후에도 취약점과 보안 설정의 변경을 지속적으로 추적한다.

## 9.2. 감시 대상

- 신규 CVE 등록
- CISA KEV 변경
- 패키지 및 이미지 digest 변경
- IAM 정책 수정
- 공개 네트워크 설정 변경
- 런타임 보안 설정 변경
- 데이터 저장 위치 변경
- 비밀정보 접근 정책 변경
- 감사 로그 설정 변경
- 백업 정책 변경
- 이전 이후 보안 통제 불일치
- 기존 취약점 재발

## 9.3. 재평가 조건

```text
신규 취약점 또는 설정 변경 감지
    ↓
영향받는 자산 식별
    ↓
현재 노출 및 위험 평가
    ↓
기존 보안 기준과 비교
    ↓
변경 제안
    ↓
격리 검증
    ↓
승인된 운영 적용
    ↓
재검사 및 기록
```

모든 변경을 자동으로 운영 환경에 적용하지 않는다.

자동 탐지·분석·리허설과 실제 운영 변경의 권한은 분리한다.

---

# 10. Security-Preserving Migration

## 10.1. 정의

**Security-Preserving Migration은 운영 시스템을 다른 클라우드 또는 온프레미스 환경으로 이전하면서 원본 환경에서 요구되던 보안 속성을 목적지에서도 충족하도록 검증하는 이전 방식이다.**

이전 성공은 애플리케이션이 새로운 환경에서 실행된다는 사실만으로 판단하지 않는다.

기존 보호 정책과 취약점 개선 상태가 목적지에서도 유지되어야 한다.

## 10.2. 기존 이전 방식과 차이

**일반적인 이전 검증**

> AWS에서 동작하던 시스템이 GCP나 온프레미스에서도 동작하는가?

**보안 보존형 이전 검증**

> AWS에서 요구되던 보호 수준과 취약점 개선 상태가 GCP나 온프레미스로 이전된 후에도 유지되는가?

## 10.3. 환경별 보안 통제 매핑

| 보안 요구 | AWS | 온프레미스 |
|---|---|---|
| 네트워크 접근 통제 | Security Group | Firewall / NetworkPolicy |
| 비밀정보 관리 | Secrets Manager | OpenBao |
| 워크로드 신원 | IAM Role | SPIFFE/SPIRE |
| 감사 로그 | CloudTrail | 중앙 감사 수집기 |
| 암호화 키 | AWS KMS | 외부 KMS / Vault 계열 |
| 취약점 평가 | Inspector·Trivy | Trivy 및 별도 진단 |
| 런타임 방어 | GuardDuty | Falco / Tetragon |
| 데이터 백업 | RDS Snapshot | PostgreSQL Backup / PITR |

위 표는 제품별 기능을 단순히 일대일 치환한다는 뜻이 아니다.

서로 다른 기술이 실제로 동일한 보안 요구를 충족하는지는 별도의 정책 평가와 기능 검증이 필요하다.

예를 들어 AWS IAM Role을 SPIFFE 인증서로 대체하는 것만으로 동등한 권한 통제가 구현되는 것은 아니다.

## 10.4. 보안 보존형 이전 절차

```text
Source Security Baseline
    ↓
Required Security Properties 추출
    ↓
Target Capability Mapping
    ↓
Security Gap Analysis
    ↓
AI 보완 패치·인프라 구성 제안
    ↓
격리 이전 리허설
    ↓
Source / Target Security Parity Test
    ↓
정책 검증 및 승인
    ↓
단계적 이전
    ↓
기능·데이터·보안 검증
    ↓
지속적인 재진단
```

## 10.5. Security Parity Test

이전 전후 환경에서 다음을 비교한다.

| 비교 대상 | 검증 내용 |
|---|---|
| 신원 | 동일한 사용자·서비스 접근 요구 충족 |
| 권한 | 불필요한 권한 증가 없음 |
| 네트워크 | 승인되지 않은 연결 거부 |
| 비밀정보 | 비밀 원문 노출 없음 |
| 암호화 | 저장·전송 중 보호 요구 충족 |
| 감사 | 필수 변경·접근 이벤트 추적 가능 |
| 취약점 | 기존 수정 사항 유지 |
| 데이터 | 정합성·위치 요구 충족 |
| 백업 | 목적지에서 복구 가능 |
| 운영 | 필수 기능·가용성 충족 |

---

# 11. 실제 침해사고 기반 보완 근거

| 사건·연구 | 기존 대응 | 추가 보완 |
|---|---|---|
| SK텔레콤 | 에이전트·네트워크 격리 | 계정·서버 설정의 지속 진단과 개선 검증 |
| Salesloft Drift | 연합 신원·최소 권한 | 외부 연결 권한 드리프트 및 회전 후 기능 검증 |
| Shai-Hulud | 격리 빌드·SBOM·서명 | 의존성 패치·재빌드·재서명 |
| SharePoint ToolShell | 서버 격리·재구축 | 패치 또는 재구축 경로 자동 판단 지원 |
| AWS Ops Wheel | XSS·세션 보호 | 코드 수정·브라우저 보안 회귀 검사 |
| Marks & Spencer | 운영 연속성·백업 복구 | 패치·이전 후 연속성과 복구 가능성 검증 |
| Cloudflare DDoS | WAF·Shield·Rate Limit | 노출 설정 개선·부하 재검증 |
| Anthropic AI 캠페인 | AI와 실행 권한 분리 | 악성 도구 호출 차단·정책 재검증 |
| GitHub MCP 연구 | Prompt Injection 차단 | 패치 생성 과정의 외부 입력·diff 검증 |

---

# 12. 신규 보안 요구사항

| ID | 요구사항 | 완료 조건 |
|---|---|---|
| SEC-PATCH-01 | 취약점·보안 설정 지속 조사 | 자산·취약점 연결 근거 |
| SEC-PATCH-02 | 위험 기반 수정 우선순위 | 노출·중요도·악용 가능성 반영 |
| SEC-PATCH-03 | AI 패치 생성 | 변경 diff 및 근거 기록 |
| SEC-PATCH-04 | 격리 리허설 | 기능·보안 회귀 검증 |
| SEC-PATCH-05 | 패치 적용 승인 | 계획 해시·승인 주체 검증 |
| SEC-PATCH-06 | 운영 적용 및 복구 | 단계적 배포·복구 가능성 검증 |
| SEC-PATCH-07 | 적용 후 취약점 재검사 | 기존 문제 해소 증거 |
| SEC-PATCH-08 | 이전 전후 보안 비교 | 필수 보안 속성 충족 |
| SEC-PATCH-09 | 취약점·설정 드리프트 탐지 | 변경 발견 시 재평가 |
| SEC-PATCH-10 | 지속 개선 이력 | 진단·수정·검증 추적 |

기존 SG-1~3, CC-01~12 및 실행 게이트와 연계하고 의미가 중복되는 통제는 통합한다.

---

# 13. 실증 시나리오

## 13.1. 대상 시스템

운영 중인 Node.js + PostgreSQL 애플리케이션을 대상으로 한다.

**원본 환경**

- AWS ECS
- Amazon RDS PostgreSQL
- 기존 인증 라이브러리
- IAM 역할
- 네트워크 접근 정책
- 실제 데이터 읽기·쓰기

**가정한 문제**

- 오래된 인증 라이브러리
- 과도한 IAM 권한
- 클라우드 종속적인 설정
- 목적지 인증·비밀관리 체계의 차이

## 13.2. 실증 흐름

### 1단계. 자동 진단

운영 애플리케이션의 소스·의존성·IAM·네트워크를 조사한다.

취약점과 이전에 필요한 변경을 구분하고 근거를 표시한다.

### 2단계. AI 패치 및 이전 계획

AI가 취약한 의존성 수정안, 권한 축소안, 목적지 환경에 필요한 설정 변경을 제안한다.

### 3단계. 격리 리허설

수정된 애플리케이션을 격리 환경에서 빌드·실행한다.

기능·보안·데이터 연결·이전 호환성을 검증한다.

### 4단계. 정책 검증과 승인

계획 해시, 대상 계정, 이미지 digest, 보안 검증 결과를 확정하고 승인받는다.

### 5단계. 단계적 이전

승인된 변경을 실제 목적지 환경에 적용한다.

데이터 이전과 트래픽 전환을 검증 가능한 단계로 실행한다.

### 6단계. 이전 후 검증

기능·데이터·보안 통제·복구 가능성을 다시 확인한다.

## 13.3. 검증 결과 화면 예시

| 항목 | 이전 전 | 이전 후 기대 결과 |
|---|---|---|
| 애플리케이션 | 정상 운영 | 정상 운영 |
| 취약점 | 발견됨 | 수정 및 재검사 통과 |
| 데이터 | 기준 데이터 | 정합성 확인 |
| 비밀정보 | 기존 보호 정책 | 동등 보호 요구 충족 |
| IAM 권한 | 과도한 권한 발견 | 최소 권한 검증 |
| 네트워크 | 기존 접근 정책 | 동등 접근 정책 |
| 복구 | 기존 복구 경로 | 목적지 복구 시험 |

위 결과는 목표 검증 기준이며 실제 시험을 통과한 결과로 간주하지 않는다.

---

# 14. 기존 보안 아키텍처와의 통합

## 14.1. 유지할 구성

- 조직·앱 단위 권한 분리
- STS AssumeRole / WIF
- 온프레미스 mTLS Agent
- 격리 빌드·리허설 샌드박스
- 독립 Policy Engine
- 승인된 실행 계약
- 영속 작업 원장
- 외부 실행 결과 재확인
- 단계적 트래픽 전환
- 데이터 정합성 검증
- 복구 및 감사 증거

## 14.2. 보완할 구성

- Vulnerability Intelligence
- Exposure Analyzer
- AI Remediation Planner
- Patch Rehearsal
- Change Impact Analysis
- Security Parity Test
- Continuous Revalidation

## 14.3. 아키텍처 통합 원칙

새로운 기능을 각각 독립된 배포 엔진으로 구현하지 않는다.

보안 패치와 마이그레이션은 기존 작업 관리자·정책 엔진·실행 계약·클라우드 어댑터·감사 체계를 재사용한다.

작업 종류에 따라 검증 조건과 승인 정책만 분기한다.

```text
Change Request
    |
    +-- Security Patch
    |
    +-- Infrastructure Configuration Change
    |
    +-- Application Modernization
    |
    +-- Cloud Migration
    |
    +-- Recovery Operation
             |
             v
      Unified Policy Gate
             |
             v
      Isolated Rehearsal
             |
             v
      Approved Execution Contract
             |
             v
      Execution Controller
             |
             v
      Verification & Audit
```

---

# 15. 발표 및 설계 설명 방향

## 15.1. 설계의 핵심 메시지

SoftBank가 발표한 Patching as a Service는 AI를 활용해 취약점을 발견하고 수정안을 검증한 뒤 안전한 변경으로 연결하는 접근방식을 강조한다.

이 접근방식에서 착안하여, 취약점을 수정하는 과정뿐 아니라 운영 시스템을 다른 클라우드나 온프레미스 환경으로 이전하는 과정에서도 기존 보안 수준을 유지하고 검증하는 구조를 설계한다.

AI 기반 취약점·의존성 분석, 격리된 패치 및 이전 리허설, 정책 기반 승인, 단계적 실행, 이전 전후 보안 통제 비교, 지속적인 재진단을 하나의 파이프라인으로 연결한다.

목표는 단순히 애플리케이션을 다른 환경에서 실행시키는 것이 아니라, **보안 개선과 운영 연속성을 유지하면서 클라우드 종속성을 낮추는 것**이다.

## 15.2. 표현상의 주의사항

| 표현 | 사용 판단 |
|---|---|
| SoftBank의 서비스를 구현했다 | 실제 동일 기능을 구현하지 않았다면 부정확 |
| SoftBank의 기술을 계승했다 | 공식 기술 제휴·계승 관계를 암시할 수 있음 |
| Patching as a Service의 접근방식에서 착안했다 | 적절 |
| 취약점 진단·수정·검증 사이클을 멀티클라우드 이전에 적용했다 | 적절 |
| AI 기반 보안 개선과 안전한 마이그레이션을 통합했다 | 적절 |

---

# 16. 결론

본 설계의 목적은 기존 멀티클라우드 관리 플랫폼을 단순한 패치 서비스로 변경하는 것이 아니다.

**Patching as a Service의 지속적인 취약점 개선 접근방식을 기존의 안전한 시스템 이전·변경 체계에 확장 적용하는 것**이다.

세 가지 핵심 개념을 하나의 아키텍처로 통합한다.

### 1. AI-Assisted Patching

취약점과 설정 문제를 발견하고, AI가 수정안을 제안하며, 독립적인 검증을 통해 개선 여부를 판단한다.

### 2. Security-Preserving Migration

시스템 이전 전후에 필수 보안 요구와 취약점 개선 상태가 유지되는지 검증한다.

### 3. Continuous Verification

운영 환경의 변경과 새로운 위협을 감시하고 지속적으로 보안 상태를 재평가한다.

이를 통해 다음 세 가지 질문에 답하는 플랫폼을 목표로 한다.

> **운영 시스템의 취약점과 환경 종속성을 함께 개선할 수 있는가?**
>
> **개선된 보안 수준을 유지하면서 다른 클라우드나 온프레미스로 이전할 수 있는가?**
>
> **이전 이후에도 보안·기능·데이터·복구 가능성을 지속적으로 검증할 수 있는가?**

---

# 부록 A. 공식 참고자료

## SoftBank

- [SoftBank — Patching as a Service 공식 발표](https://www.softbank.jp/en/corp/news/press/sbkk/2026/20260714_02/)
- [SoftBank 공식 기업 뉴스](https://www.softbank.jp/en/corp/news/)

## 취약점 및 위협 정보

- [CISA Known Exploited Vulnerabilities Catalog](https://www.cisa.gov/known-exploited-vulnerabilities-catalog)
- [NIST National Vulnerability Database](https://nvd.nist.gov/)
- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [OWASP API Security Top 10](https://owasp.org/API-Security/)
- [OWASP GenAI Security Project](https://genai.owasp.org/)

## 클라우드 및 운영 보안

- [AWS IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html)
- [Google Cloud Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation)
- [Open Policy Agent](https://www.openpolicyagent.org/docs)
- [OpenTelemetry](https://opentelemetry.io/docs/)
- [SPIFFE/SPIRE](https://spiffe.io/)

## 소프트웨어 공급망

- [SLSA](https://slsa.dev/)
- [Sigstore/Cosign](https://docs.sigstore.dev/)
- [Trivy](https://trivy.dev/)

---

# 부록 B. 설계·구현 검증 추적 기준

| 항목 | 내용 |
|---|---|
| Requirement ID | SEC-PATCH-01~10 |
| Threat Reference | 관련 공격·취약점 |
| Security Goal | SG-1~3 |
| Existing Control | 기존 CC-01~12 |
| Target Component | 적용 대상 서비스 |
| Proposed Change | 코드·설정·IaC 변경 |
| AI Responsibility | AI가 수행하는 분석·제안 |
| Execution Authority | 실제 실행 권한 주체 |
| Policy Gate | 필수 정책 검사 |
| Rehearsal Evidence | 사전 검증 증거 |
| Approval Evidence | 승인 주체·계획 해시 |
| Deployment Evidence | 적용 결과 |
| Security Parity | 원본·목적지 보안 비교 |
| Recovery Evidence | 복구 시험 |
| Validation Status | 설계·구현·모의 검증·실환경 검증 |

실제 구현 상태와 검증 상태는 구분하여 관리한다.
