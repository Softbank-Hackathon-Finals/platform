# 보안 설계 고려사항

> **작성자:** 이상무 (보안 통제 · 게이트 담당)
> **원본:** 문서마다 아래 표에 적었다. 노션이 원본인 문서는 2026-11-03 기준 사본이라 고칠 때 노션을 먼저 고치고, 레포가 원본인 문서는 여기서 고친다.
> **상태:** 추후 고려사항. 현재 작업(T5 등 `docs/tasks.md`의 스켈레톤)의 범위를 바꾸지 않는다.

`docs/tasks.md`의 보안 작업은 최소 구색을 갖추기 위한 스켈레톤이다(11/2 회의). 이 폴더는 담당자가 그 너머로 고려한 보안 설계를 남겨 둔다. 설계 문서에 반영할 때는 `scripts/design-bump.sh`로 버전을 올리고 ADR을 함께 쓴다.

| 문서 | 노션 원본 | 내용 |
|---|---|---|
| [사고 기반 멀티클라우드 보안 아키텍처](incident-based-architecture.md) | [v1](https://app.notion.com/p/v1-40c8bee9ada48020b86ff86df8f59e0f) | 실제 침해사고 9건 분석 → 통제 CTRL-01~09, 정책 POL-01~12, 신뢰 경계, 실행 계약, 검증 시험 T-01~10 |
| [Patching as a Service 기반 지속적 보안 개선](continuous-secure-modernization.md) | [Patching as a Service](https://app.notion.com/p/Patching-as-a-Service-40c8bee9ada4804ca2b9d33928e33c98) | 취약점 진단 · AI 수정 · 격리 검증 · 재진단 순환을 이전에 적용, Security Parity Test, SEC-PATCH-01~10 |
| [보안 탐지 통합 (T33)](detection-integration.md) | 없음 (**레포가 원본**) | AWS · Google Cloud · 온프렘 탐지 결과를 OCSF 부분집합으로 정규화해 한 화면에. 진행 중인 작업 T33의 설계 |

## 지금 설계 문서와의 관계

| 이 폴더 | 설계 문서 (v0.1) |
|---|---|
| Security Parity Test (이전 전후 보안 비교) | 8.5 원본 대조 게이트 · 6개 통제 (T5 · T14 · T19) |
| CTRL-08 AI는 권한 주체가 아니다, CTRL-09 프롬프트 인젝션 차단 | 1장 · 8.3 설계 원칙 (에이전트에게 실행 권한을 주지 않는다) |
| 감사 증거 보호 (10.3) | T24 감사 로그 (S3 Object Lock) |
| CTRL-01~07, 멀티테넌시, Policy-as-Code, 지속 재진단 | 아직 없음 |

## 쓰기 전에 확인할 것

인용한 사고 · 수치 · CVE · 발표(예: CVE-2026-18481, SoftBank Patching as a Service 발표, 각 사고의 피해 규모)는 발표 자료나 설계 문서에 넣기 전에 원문을 열어 확인한다(T23과 함께).
