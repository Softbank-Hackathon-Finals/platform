# T10 운영자 신원과 계획별 권한 연결 제안

이 문서는 기존 CUTOVER 접수 어댑터에 연결할 **검증 코드와 호스트 인터페이스**다.
T21의 로그인 제공자 선정·발급 설정이나 운영 인증 완료 기록이 아니다.
정본 `docs/design.html` 7.5의 로그인 요구와 8.9의 승인 결속, T10/T21 태스크를 구현하는
후속이며, 실제 T19·SHIFT authority를 만들거나 대신 판정하지 않는다.

## 원본에서 확인한 계약과 아직 필요한 값

`docs/tasks.md` T21과 #27은 승인 버튼에 결속 값을 표시하도록 요구한다.
설계 7.5는 콘솔 로그인을 요구한다. 현재 develop에는 플랫폼 운영자용 issuer,
audience, subject 발급 계약이나 콘솔 로그인 구현이 없다. T33 내부 수집 토큰과
T38 ChatGPT 구독 자격은 사람의 플랫폼 승인 자격이 아니다.

| 제공 주체 | 실제로 필요한 입력 | 이 PR 상태 |
|---|---|---|
| T21/인증 담당 | 기존 IdP의 HTTPS issuer, 플랫폼 전용 audience, 콘솔 client ID, 승인에 필요한 인증 수준(ACR), 공개 JWK와 교체 절차 | 실환경 값 미확인. verifier에 명시적으로 주입해야 함 |
| 운영자 관리 담당 | 실제 사람 계정의 안정된 issuer/subject와 tenant 매핑, 계획별 권한 부여·회수 결정 및 감사 참조 | HTTP 자동 등록 없음. 신뢰된 관리 경로에서만 기록 |
| T19 | 서버 정본 GateComparison 전체와 15필드 context, 현재 예외·취소·보안 hold 및 결속 검증 | 기존 upstream authority가 필수. 기본 거부 |
| T17/T18/T36 | 실제 SHIFT100/반영·지연 조건, 동일 계획·창·프로필에 결속한 판정 | 모의 값으로 대체하지 않음 |
| T12/T7 | TLS Executor와 전용 CA, 고정 환경/connection_ref | 기존 CUTOVER 계약 유지 |

T36 PR #137의 `build_window/receive/judge`는 실제 authority 연결 때 검토할 공통
판정 구현 후보다. 이 PR은 그 미병합 코드를 복사하거나 실제 SHIFT 영수증으로 사용하지 않는다.

## 신원 검증

`OperatorTokenVerifier`는 [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068.html)의
access token 형식을 받는 선택형 어댑터다. 팀이 이 로그인 제공자를 이미 채택했다는 뜻이
아니다. 실제 제공자 계약이 다르면 별도 검토한 어댑터로 연결하며 검사 항목을 끄지 않는다.

- host가 고정한 issuer/audience/client/ACR와 **공개** RSA JWK만 사용한다.
  토큰 header나 요청 URL로 키를 받지 않는다. RS256과 `at+jwt` 타입을 고정한다.
  [PyJWT API 지침](https://pyjwt.readthedocs.io/en/stable/api.html)에 따라 허용 알고리즘을 토큰에서 정하지 않는다.
- 서명, 정확한 발급자·단일 audience, `sub/iat/nbf/exp/auth_time/jti/client_id/acr`를 검사한다.
  기본 토큰 수명은 최대 5분, 최근 사람 인증은 15분 이내다. host 설정도 각각 15분/1시간 이하로 제한한다.
- host가 실제 사람으로 확인해 등록한 subject만 서버 tenant에 매핑한다.
  이메일·UI actor·헤더 tenant·토큰 roles는 권한 근거로 쓰지 않는다.
  내부 actor ID는 issuer/subject를 해시한 불투명 식별자다.
- Bearer 헤더 하나만 받는다. 중복 헤더, ID token, 다른 audience의 서비스 토큰,
  미등록 subject, 잘못된 서명·시간·키는 모두 신원을 반환하지 않는다.
- 토큰/개인 키를 DB·로그·응답에 저장하지 않는다. 테스트는 매 실행 생성한 서명 키만 사용한다.

TLS와 실제 브라우저 로그인/PKCE·토큰 수명 관리는 T21 호스트 책임이다. 이 라이브러리는
로그인 서버, 비밀번호 저장소, 쿠키 세션 또는 새 클라우드 IdP를 만들지 않는다.
SSE를 포함해 Bearer를 보낼 수 있는 같은 출처의 클라이언트가 필요하며 토큰을 URL에 넣지 않는다.
공개 키 교체는 host 설정 배포로 수행한다. IdP의 전역 로그아웃·실시간 introspection은
구현하지 않았다. 즉시 운영 권한을 회수하려면 아래 계획별 권한을 먼저 회수해야 한다.

## 계획별 현재 권한과 승인 취소

`OperatorAccess`는 기존 `plan_commands`에 append-only 이력을 저장한다.
새 테이블·마이그레이션·DB 역할·관리 HTTP API는 없다.

| 동작 | 저장/검사 |
|---|---|
| `set_permissions` | trusted host가 실제 관리 결정을 인증한 뒤 호출. plan/tenant/subject, 권한, 유효 구간, 기대 revision, 관리 감사 참조를 저장 |
| 권한 | `read`, `approve_cutover`, `execute_cutover`를 구분. 쓰기 권한은 `read`를 함께 요구 |
| 권한 회수 | 빈 권한 집합을 다음 revision으로 기록. 회수 후 재부여도 새로운 revision |
| 재인증 요구 | `valid_after`보다 먼저 발급된 토큰 거부 |
| `revoke_approval` | 아직 소비하지 않은 승인 ID의 취소를 영속화. 이미 접수된 원격 실행을 취소했다고 반환하지 않음 |
| 조회/SSE | 현재 계획의 read 권한과 유효 토큰 재검사. 다른 계획/tenant로 권한 전파 없음 |
| 승인 접수 | 동일 plan lock 안에서 실제 신원, 승인 권한, upstream authority 확인 |
| 실행 접수 | 실행자의 실행 권한 + 원 승인자의 현재 승인 권한 + 승인 취소 상태를 동일 lock 안에서 확인 |

`administration_ref` 문자열 자체는 관리자 인증이 아니다. host가 현재 관리자 권한을
검증하고 남긴 실제 결정의 참조여야 하며, 클라이언트 JSON에서 이 관리 함수를 호출하면 안 된다.
실제 관리 경로/계정 매핑이 아직 없으므로 이 PR에서 운영 권한을 부여하지 않는다.
서버 내부 Python 호출은 신뢰 경계다. 임의 Python/DB 쓰기 권한을 가진 코드를
이 라이브러리가 독립적으로 격리한다는 뜻이 아니다.

승인 context revision에는 upstream revision과 승인자의 권한 revision을 함께 결속한다.
따라서 승인 후 권한 회수·재부여를 거치면 옛 승인은 다시 쓸 수 없다.
실행자는 승인자와 달라도 되지만 각각 필요한 권한을 현재 가지고 있어야 한다.
잠금을 기다리다 만료된 토큰과 upstream 조회 중 만료된 토큰도 거부한다.
권한 수정·취소와 승인 소비가 같은 계획 잠금을 쓰므로 먼저 commit한 변경을 뒤의 접수가 본다.
접수가 먼저 commit됐으면 이후 회수가 그 원격 효과를 되돌리지는 않는다.

## 호스트 연결

`create_operator_cutover_app(store, executor, bindings=..., verifier=..., authority=...)`
한 곳에서 resolver, 현재 read 검사, 권한 검사와 기존 T19/SHIFT authority를 연결한다.
`authority`를 생략하면 여전히 거부한다. 기존 `create_cutover_app`의 기본 인증 거부도 유지한다.
순수 `Principal`이나 서명 확인 없이 만든 HTTP actor 값은 이 연결의 사람 자격으로 받아들이지 않는다.

권한 변경은 트랜잭션이 먼저 저장된 뒤 효력을 갖는다. DB 재시작 뒤에도 그대로 읽으며
SQLite `BEGIN IMMEDIATE`와 PostgreSQL의 기존 plan advisory lock을 사용한다.
권한이 네트워크 호출 중 회수되면 이미 저장한 실행 명령은 남고 응답/관측 접근은 거부될 수 있다.
권한 있는 운영자가 기존 GET 복구로 이어 읽어야 하며 POST를 다시 보내지 않는다.

## 검증 범위

로컬은 임시 PostgreSQL과 SQLite, 실제 생성 키의 서명/변조·만료·잘못된 audience,
HTTP 승인/조회/SSE, 권한 회수/재부여·승인 취소·잠금 경쟁을 시험한다.
AWS는 기존 EC2의 비루트 임시 프로세스에서 같은 코드와 합성 identity/gate/SHIFT를 시험한다.
두 시험 모두 실제 사람 로그인·운영 권한 부여·게이트 PASS·실제 컷오버 증거가 아니다.
[검증 기록](../evidence/t10-operator-access-2026-11-04/README.md)을 함께 본다.
