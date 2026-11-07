# T14 출발지 demo-gate:1 실제 실행: 세 시험 PASS

2026-11-07 20:46 KST, 실제 출발지(AWS, `source-dev`)에서 `operations/controls/run.py` 한 번으로 `demo-gate` v1 세 시험을 실행했다. 운영자 본인 SSO 세션으로 실행했고, 걸린 시간은 10초다. 등록 파일은 운영자 비공개 폴더에 두었다. **GATE에는 기록하지 않았다**(계획 · context에 묶지 않은 확인 실행).

| 시험 | 어댑터 | 결과 | 관찰 | 대조군 | 시간 |
|---|---|---|---|---|---|
| IAM-01.1 | `aws-object` | **PASS** | 익명 GET 거부(deny) | 앱 역할 GET 성공(pass) | 1.2초 |
| SEC-01.1 | `aws-secret` | **PASS** | 비교 역할(`t14-other`) 읽기 거부(deny) | 앱 역할 읽기 성공(pass) | 1.8초 |
| AUD-01.1 | `pod-log` | **PASS** | 관리자 로그인 기록 있음(present) | 해당 없음 | 7.3초 |

- **IAM-01.1 · SEC-01.1:** `source-workloads`에 선언된 T14 자원을 썼다. 앱 역할과 `t14-other`는 팀 SSO 역할의 `bravo-t14-*` 세션만 신뢰한다. 카나리 객체 `attachments/t14-canary.txt`는 버전을 고정했다. T14 시크릿은 자격이 아닌 고정 값이다. 운영자 프로필이 각 역할을 15분 세션으로 넘겨받고, STS 신원이 등록 RoleId와 같은지 확인했다.
- **AUD-01.1:** [ADR-0037](../../adr/0037-aud-login-pod-log-source.md) 경로다(CNPG primary 자체 로그). 출발지 EKS API는 SSM 포트포워딩으로 붙었다. 중앙 감사 로그 검색이 아니다.
- 증거 파일 4개(실행 원문과 시험별 증거)는 운영자 비공개 폴더(0600)에 있다. 여기에는 시크릿 값 · 임시 자격 · 계정 식별자 · ARN이 없음을 확인했다.

목적지 쪽 IAM-01.1(카나리 객체 · client 자격)과 SEC-01.1(T9 `t14-other`)은 아직 실행하지 않았다.
