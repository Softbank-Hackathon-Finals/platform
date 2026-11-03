# T10 운영자 권한 연결 검증 · 2026-11-04

## 확인한 범위

| 환경 | 결과 | 범위 |
|---|---|---|
| 로컬 | 383 passed, Ruff PASS | 전체 platform-events 회귀, 실제 임시 PostgreSQL/SQLite, 생성 RSA 키, HTTP/SSE, 권한·승인 취소·잠금 경쟁 |
| AWS 기존 EC2 | 177 passed | 비루트 임시 프로세스, 합성 IdP/gate/SHIFT, 임시 SQLite, loopback HTTP/TLS |
| 공개 자료 | privacy check / diff check PASS | 키 값·개인 이메일·운영 식별자 없는 공개 기록 |

AWS 실행은 2026-11-04 00:19:24–00:19:43 KST이며 `aws-test.json`에는 UTC 시각을 보존했다.
기존 계정·대상 소유 경계를 먼저 대조했고 새 자원을 만들지 않았다.
임시 디렉터리는 제거됐으며 운영 DB, Kubernetes, Cloudflare에는 접근/변경하지 않았다.
공개 JSON의 `source_files`는 실행한 소스/시험 입력 각각의 SHA-256이다. 제출본/원본 실행 응답은
비공개 작업 기록에 보존하고 원격 결과·현재 소스 바이트와 모두 대조했다.

## 핵심 반례

- 다른 키/발급자/audience/client/ACR, ID token/unsigned/symmetric token, 미등록 subject,
  오래된 인증·만료·미래 토큰, 헤더가 지정하는 키 URL, 중복 Authorization을 거부한다.
- 유효 서명이라도 계획별 권한이 없으면 조회/SSE/승인/실행을 거부한다.
- 승인자는 승인 권한, 별도 실행자는 실행 권한을 각각 요구한다. 승인자 권한 회수,
  회수 후 재부여, 승인 취소, 계획/tenant 변경은 원격 POST 전에 거부한다.
- 동일 계획 잠금을 기다린 접수는 그 앞에서 commit된 회수와 현재 시각을 본다.
  upstream 조회 중 만료된 토큰도 거부한다. 저장 실패는 전체 권한 변경을 되돌린다.
- 유효 사람 토큰과 권한만으로 T19/SHIFT가 없을 때 실행할 수 없다.
- 접수 뒤 승인 취소가 실제 원격 실행을 취소했다고 반환하지 않는다. 회수 뒤에는
  같은 명령 재조회에도 현재 read 권한이 필요하다.

## 아직 확인하지 않은 것

실제 사람 로그인/IdP 발급, 실제 관리자의 권한 부여·회수, 운영 T19/SHIFT 정본 소비,
플랫폼 서비스 배포와 실제 컷오버는 미검증이다. 생성 시험 키와 합성 authority를 그 증거로
사용하지 않는다. T21의 실제 issuer/client/audience/ACR와 사람 subject 매핑·관리 경로가 필요하다.
자세한 연결 조건은 [계약](../../contracts/T10-operator-access-v1-proposal.md)에 있다.

첫 실행의 381/176 결과는 `aws-test-initial.json`과 비공개 기록에 보존한다. 그 뒤 refresh된 토큰의
발급 시각 대신 실제 `auth_time` cutoff를 검사하고, upstream 조회 뒤 결속 만료를 다시 확인하는
반례를 추가했다. 위 383/177은 그 수정까지 포함한 최종 소스의 재검증이다.
