# T14: 앱 → PgBouncer TLS 시험

`postgres_tls.PoolerTLSProbe`는 T5의 ENC-01.1·01.2·01.3을 실제 `psql`
접속으로 관측한다. 설정에 TLS가 적혀 있다는 것만으로 PASS를 만들지 않는다.
현재 자격 범위는 PostgreSQL 18 클라이언트, SCRAM 인증, C 로케일, Linux/macOS다.

| 시험 | 실행과 관측 |
|---|---|
| ENC-01.1 | `verify-full`, 최소 TLS 1.2로 접속하고 클라이언트 `\conninfo` 및 고정 `SELECT 1` 결과 확인 |
| ENC-01.2 | 정상 접속 → 다른 CA와 다른 hostname을 각각 시험 → 정상 재접속. 두 음성 시험이 모두 해당 인증서 검증 오류여야 `deny` |
| ENC-01.3 | 정상 접속 → `sslmode=disable` → 정상 재접속. PgBouncer의 SSL 필수 또는 PostgreSQL의 비암호화 HBA 거부만 `deny` |

잘못된 비밀번호, 연결 거부, EOF, 알 수 없는 오류, 시간 초과는 암호화 정책의
효과를 입증하지 않는다. 대조군 실패는 `POSITIVE_CONTROL_FAILED`, 그 외
불명확한 결과는 `TOOL_ERROR`·`TIMEOUT`·`UNEXPECTED_RESPONSE`로 남긴다.
틀린 CA나 hostname 또는 평문 접속이 성공하면 통제 실패(`allow`)다.

## 실행 경계

등록은 서버가 소유한다. 사용자 업로드·AI 출력으로 실행 파일, 목적지 또는
관측 결과를 등록하지 않는다. T10/T14 배치 연결에서 다음을 강제해야 한다.

1. 앱 네임스페이스의 전용 검사 Pod를 사용하고 실제 Pod UID, Pooler UID,
   앱·환경·등록 revision을 신뢰할 수 있는 Kubernetes 조회와 결속한다.
   문자열로 적힌 namespace나 UID만으로 해당 위치에서 실행했다는 증명이 되지 않는다.
2. 검사 Pod에는 서비스 계정 토큰, 클라우드 자격증명, 호스트 소켓을 주지 않는다.
   앱 역할의 단기 자격만 주입하고 Pod 종료 시 폐기한다. 읽기 전용 루트 파일시스템,
   non-root, capabilities 제거, seccomp, CPU/메모리 및 전체 작업 기한을 적용한다.
3. 실행 이미지와 `psql`·연결 라이브러리의 버전/digest를 자격 시험으로 고정한다.
   실행 파일 해시만으로 동적 라이브러리까지 검증했다고 주장하지 않는다.
   등록된 실행 파일과 부모 경로는 앱이 수정할 수 없어야 한다.
4. 단일 등록 IP로만 연결한다. 정상·음성 대조군에서 주소, 포트, DB와 역할을
   유지하고 CA 또는 hostname 하나만 바꾼다. DNS 실패를 인증서 거부로 세지 않는다.
5. 네트워크 정책으로 검사 Pod의 목적지를 제한한다. 이 라이브러리는 네트워크
   방화벽이나 등록 API의 인증·권한 검사를 대신하지 않는다.

자격증명 제공자는 호출 context를 확인하고 앱 비밀번호를 메모리로 반환한다.
비밀번호는 짧게 실행되는 자식 `psql`의 환경변수로만 전달한다. 명령행, 임시
파일, 증거, 로그에는 기록하지 않는다. **프로세스 환경을 읽을 수 있는 동일 UID나
노드 관리자로부터 비밀번호를 보호하는 방식은 아니므로** 전용 Pod와 실행 주체
분리가 필요하다. 비밀번호 기반으로 임의 앱의 인증 방법을 바꾸는 기능도 아니다.

기존 환경변수·`.psqlrc`·비밀번호 파일·GSS 인증을 상속하지 않는다. `require_auth`
는 SCRAM만 허용한다. 평문 음성 시험에서 서버가 cleartext password를 요구하면
자격증명을 보내지 않고 실패한다. mTLS·다른 인증 경로는 별도 자격 시험이 필요하다.

## 제한과 증거

- 각 프로세스는 벽시계 10초, 연결 5초, stdout+stderr 합계 64 KiB로 제한한다.
  전체 Pod/워크플로 기한은 호출 측이 추가로 제한한다.
- SQL은 `SELECT 1`로 고정한다. 업무 데이터 읽기·쓰기나 임의 SQL을 받지 않는다.
- 증거에는 실행 context, 등록/자원 결속 해시, 클라이언트 해시·버전,
  대조군별 정규화 결과와 시각만 기록한다. 원문 오류·접속 주소·DB명·사용자명은 제외한다.
  증거 저장 실패 시 성공 관측을 반환하지 않는다.
- `PoolerTLSProbe`는 업로드된 JSON을 검증하거나 승인하는 API가 아니다.
  보관된 증거의 서명/권한/TTL 및 최종 보안 게이트는 기존 T19 계약을 따른다.
- **ENC-01.4(복제)와 ENC-01.5(pooler → DB)는 이 결과로 통과시키지 않는다.**
  실제 서버 측 연결과 설정을 관측할 별도 검사기가 필요하다.
- 로컬 kind의 실제 접속 성공은 팀 AWS EKS·온프렘 RKE2의 통과를 뜻하지 않는다.
  두 환경에 각각 등록·배치한 뒤 같은 프로필로 다시 시험해야 한다.

## 공식 동작 근거

- [PostgreSQL 연결 옵션](https://www.postgresql.org/docs/18/libpq-connect.html):
  `hostaddr`, `verify-full`, `require_auth`, `connect_timeout`.
- [PgBouncer TLS 설정](https://www.pgbouncer.org/config.html):
  클라이언트와 서버 TLS는 별도 설정이다.
- [CloudNativePG Pooler](https://cloudnative-pg.io/docs/1.30/connection_pooling/):
  Pooler와 인증서·앱 인증의 연결 구조.

단위 시험의 `fixtures/tls/*.pem`은 이 시험만을 위해 만든 공개 CA 인증서다.
개인 키는 저장소에 없고 운영 신뢰 저장소에 설치하지 않는다.
