# T14: 기존 앱 실행 경계의 읽기 전용 TLS 수집기

`AppProcessTLSProbe`는 **기존 앱의 실행 환경과 기존 앱 자격**을 사용하는 별도
수집기다. `PoolerTLSProbe`의 전용 검사 Pod를 대체했다고 주장하지 않으며,
단기 DB 자격 발급·독립 샌드박스·침해된 앱 내부의 신뢰성을 제공하지 않는다.

T6와 합의한 범위는 기존 앱 경계의 읽기 전용 관측까지다. 실제 공유 DB 쌍을
호출하기 전에는 T12와 PAUSE·펜싱·라우팅 전환이 겹치지 않는 실행 창을 확보한다.
새 Pod, 공유 Secret 반출, 역할/RBAC/네트워크 변경은 이 경로에 포함하지 않는다.

## 수집 방법

| 시험 | 실제 동작 | 실패와 미확인 구분 |
|---|---|---|
| ENC-01.1 | 고정 Python 자식에서 psycopg/libpq `verify-full`, TLS 최소 1.2, SCRAM 인증 및 `SELECT 1` | 클라이언트의 TLS 사용과 결과 1을 확인. 접속 실패는 UNKNOWN |
| ENC-01.2 | 정상 접속 → 독립 CA / 틀린 hostname TLS handshake → 정상 접속 | 해당 OpenSSL 체인 검증 오류 또는 이름 불일치만 deny. 금지 handshake가 성공하면 FAIL |
| ENC-01.3 | 정상 접속 → 비밀번호 없는 PostgreSQL startup → 정상 접속 | PgBouncer `08P01` + `SSL required`/`TLS required`만 deny. 평문 인증 challenge가 오면 FAIL. 다른 오류는 UNKNOWN |

평문 시험에는 비밀번호·SQL을 보내지 않는다. **평문에서 업무 쿼리를 실행한 시험이
아니다.** ENC-01.4 복제와 ENC-01.5 Pooler→DB는 이 결과로 통과시키지 않는다.
협상한 TLS 버전 문자열을 별도로 수집하지 않으며, 고정 libpq에 최소 버전 옵션을
전달하고 `ssl_in_use`를 확인한 범위다. 지원 클라이언트는 psycopg 3.x와 libpq 18이다.

## 서버 등록·실행 경계

1. 서버가 `AppProcessRegistration`을 소유한다. 앱·환경·등록 revision,
   실제 앱 Pod/Deployment UID·이미지 digest, Pooler UID·고정 IP/포트·DB/역할,
   CA·Secret 참조/버전의 해시, Python 실행 파일·고정 SELECT 클라이언트 해시와
   psycopg/libpq 버전을 결속한다. 앱·LLM·업로드된 JSON이 등록값을 정하지 않는다.
2. `binding_reader(context)`는 **실제 Kubernetes·신원·Secret/CA 참조 readback**을
   등록과 대조한 해시를 반환해야 한다. 등록 JSON을 그대로 해시해 반환하는 것은
   로컬 시험 fixture일 뿐 실환경 수집기로 승인할 수 없다. 전후 확인이 실패하면
   결과를 반환하지 않고 Runner는 UNKNOWN으로 남긴다. 수집은 동일 실행 ID에 묶는다.
3. 자격 제공자는 앱이 이미 쓰는 IRSA 등으로 본인 역할·현재 Secret 버전과 등록을
   검증한다. 비밀번호만 프로세스 메모리로 반환한다. 외부 검사 Pod·운영자 파일로
   공유 DB 비밀번호를 옮기거나 이 라이브러리에서 새 권한을 발급하지 않는다.
4. 정상 연결은 등록된 Python과 고정 스크립트를 `-I`로 실행한다. 실행 파일·스크립트
   해시를 먼저 확인하고 등록된 psycopg/libpq 버전은 연결 전에 검사한다. 앱이 실행
   파일과 패키지를 변경할 수 없도록 읽기 전용 이미지·호스트 실행 경계를 확보해야 한다.
   해시 두 개만으로 전체 동적 라이브러리/운영체제 무결성을 증명하지 않는다.
5. 자식에는 새 환경만 넘긴다. 부모의 AWS·모델·GitHub 자격, PG 서비스 설정을
   상속하지 않는다. 비밀번호는 짧은 자식의 `PGPASSWORD` 환경 메모리에만 있으며
   동일 UID·노드 관리자는 이를 읽을 수 있다. 별도 자격 격리로 표현하지 않는다.
6. 정상 연결은 10초·출력 64KiB, 음성 socket은 5초·응답 8KiB로 제한한다.
   전체 실행·자격 제공자·readback·보존 ACK의 기한/취소는 서버 호스트가 강제한다.
   업무 데이터·임의 SQL·사용자가 지정한 목적지는 받지 않는다.

## 증거와 보존 ACK

도구는 `bravo-app-process-tls`, 도구 버전은 `1`이다. T5의 ENC 시험 방법 `1`을
수행하는 새로운 구현으로 등록하며, 기존 psql 도구 결과와 실행 경계를 구별한다.
증거에는 실행 ID·앱/환경·시험·방법, 등록/실행 결속 해시, 클라이언트 버전/해시,
정규화한 대조군 결과만 남긴다. 원문 오류·주소·DB명·사용자명·비밀번호는 남기지 않는다.

`evidence_sink(payload)`의 반환은 다음 두 값이 모두 정확히 일치해야 한다.

```json
{"run_id": "해당 실행 ID", "output_sha256": "sha256:보존한 원문 해시"}
```

호스트는 접근 통제된 저장소에 원문과 메타데이터를 저장하고 파일·디렉터리 `fsync`
또는 동등한 내구성 보장을 확인한 **뒤** ACK를 보낸다. 단순히 받은 값을 반사하는
서버는 이 계약을 충족하지 않는다. ACK 누락·다른 실행/해시·보존 실패는 UNKNOWN이다.
해시는 서명이나 승인 권한이 아니며 T19의 출처·유효기간·승인 검사는 그대로 적용한다.

## 검증 범위

- 단위 회귀: 자원 전후 변경, 잘못된 실행 파일, ACK 누락/다른 실행,
  대조군 실패, 금지 접속 허용, 인증서 오류 종류 불일치, 비밀번호 없는 startup,
  부모 자격/PG 설정 미상속과 증거 비밀 배제.
- `BRAVO_APP_TLS_DOCKER=1 uv run pytest tests/test_app_process_tls_live.py`:
  임시 PostgreSQL 18.4 + PgBouncer 1.25.1의 실제 TLS·인증·SELECT 1·음성 접속.
  파일/디렉터리 fsync ACK가 있을 때 3개 PASS, 보존 ACK가 없으면 3개 UNKNOWN.
  나머지 full 프로필 17개는 NOT_RUN. 종료 시 이 시험의 컨테이너·네트워크·시험 키를 정리한다.
- **로컬 시험의 자원 readback은 fixture**다. 실제 앱 UID·IRSA·Secret·CA의
  서버 검증과 운영자 보존 채널을 연결한 새 실행 전에는 팀 출발지/목적지의 PASS가 아니다.

### 평문 응답의 형식 검증

인증 번호·본문 길이와 최초 SASL 메커니즘 목록의 종료를 검사한다. 초기 인증 요청으로
유효한 프레임만 평문 startup 허용으로 센다. continuation, 정의되지 않은 번호,
잘린 MD5 salt/SASL 목록은 `UNKNOWN/UNEXPECTED_RESPONSE`다.
ErrorResponse는 첫 종료 바이트 뒤의 필드를 다시 읽지 않으며, 중복/빈 필드와 필수
severity·SQLSTATE·message 누락을 거부한다. 유효한 형식에서만 등록된 TLS 필수 오류를 판정한다.
형식 기준은 [PostgreSQL 18 메시지 형식](https://www.postgresql.org/docs/18/protocol-message-formats.html)과
[오류 필드](https://www.postgresql.org/docs/18/protocol-error-fields.html)다.

로컬 실제 TLS 시험에는 같은 등록 주소의 중계기가 TLS 트래픽을 그대로 통과시키고
평문 응답만 바꾸는 경우를 추가했다. 정상 대조군 전후 성공을 유지하면서 깨진 종료/인증
프레임은 UNKNOWN, 정상 TLS 필수 응답은 PASS, 유효한 평문 인증 요청은 FAIL이어야 한다.
