# T6 로컬 사전 준비 검증 — 2026-11-03

T6 / platform#11의 후속 준비다. 기준 커밋은 `validation.json`에 기록했다. 이 결과는 local configuration 검사이며 T6 배포나 T34 감사 수집 완료가 아니다.

- 예시 구성: 선택된 18개 구성 검사 통과. `configuration-check.json`은 `runtime_verified=false`, `deployment_ready=false`를 유지한다.
- 새 시험 15개: EKS audit 대신 CloudTrail/API 로그만 켜기, 상충 audit 설정, pgAudit DDL 제외·접속 로그 끄기, 바인딩 파라미터 로그 켜기, CNPG 직접 접속, DB readiness, 정적 자격증명, 중복 env, 잘못된 pooler backend/TLS/admin, ESO 인증·Secret 결속 오류, 입력 오류의 민감값 출력 방지를 검증했다.
- 저장소 `scripts/tests` 전체 34개 통과. 변경된 Python 파일 ruff 검사 및 `git diff --check` 통과.
- [운영 가이드](../../../infra/envs/source-aws/README.md)에 T1 출력 소비·미해결 egress, T8 DB readback, T34 감사 로그 canary와 읽기 권한 인계 항목을 남겼다.
- 클라우드 계정/클러스터 readback, 자원 생성·변경, DB SQL 실행, canary, CI 실행, 외부 게시·PR·이슈 체크는 수행하지 않았다. 팀 채택 전 로컬 제안이다.

검사 명령과 검사한 파일 SHA-256은 `validation.json`에 있다. 예시 파일은 발췌 구성으로서 EKS 생성 요청이나 Kubernetes 배포 manifest가 아니다. 실제 환경 구축 후 provider schema·TLS 연결·권한 거부·로그 전달·publication과 앱 쓰기 경로를 다시 확인해야 한다.
