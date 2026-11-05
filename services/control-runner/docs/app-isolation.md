# ISO-01.1 앱 간 객체·DB 접근 격리

서로 다른 두 앱의 신원으로, 같은 대상 앱의 현재 첨부 객체와 DB에 실제 접근한다.
`AppIsolationProbe`가 두 결과를 결합하고 T14 Runner가 T5 결과를 만든다.
Secret 접근 시험(SEC-01.1), 익명 객체 읽기(IAM-01.1)와 별도이다.

## 첫 구현의 범위

- 객체: AWS S3 현재 객체의 일반 `GetObject`, 서로 다른 두 앱 역할.
- DB: 같은 등록 PostgreSQL 클러스터 안의 서로 다른 두 DB·접속 역할.
- DB 접속은 직접 연결, SCRAM-SHA-256, verify-full TLS, channel binding 필수.
  libpq 18에서 검증했으며 이전 버전은 별도 검증이 필요하다.
  제한된 ASCII DB/역할 이름을 지원한다.
- DB 권한/데이터·역할·Secret·정책을 수정하지 않는다. 실제 `CONNECT` 권한만
  검사하며 테이블별 읽기/쓰기나 전체 객체 키의 접근을 대신 증명하지 않는다.
- SeaweedFS의 역할·키 신원 검증, 버전 없는 객체, PgBouncer/NAT 경유 DB는
  아직 이 경로의 운영 자격을 갖추지 않았다.

## 객체 접근 검사

1. 서버가 두 앱·환경·서로 다른 버킷·현재 객체 버전과 IAM 역할 ARN/RoleId를 등록한다.
2. 기존 `aws_identity.verified_client`로 각 역할의 단기 자격증명과 실제 STS 신원을 확인한다.
   SDK 엔드포인트·TLS 검증·시간 제한·디버그 로그 차단 규칙을 재사용한다.
3. 대상 앱 역할로 대상 객체를, 다른 앱 역할로 다른 앱 자신의 객체를 읽는다.
4. **다른 앱 역할로 대상 앱의 동일 객체를 일반 GET**한다.
   `VersionId`를 요청하지 않는다. `GetObjectVersion`은 다른 권한이기 때문이다.
5. 양쪽 정상 읽기와 등록 정보를 다시 확인한다. 반환 버전이 현재 등록 버전과 같아야 한다.

허용 대조군이 성공하고 HTTP 403 `AccessDenied`면 거부 관찰이다.
만료된 토큰, 없는 객체, 네트워크 오류, 객체 버전 변화는 격리 성공이 아니다.
객체 본문을 읽거나 저장하지 않으며 SDK 스트림은 닫는다.

## DB 접근 검사

1. 현재 등록된 클러스터·주소·CA 해시·두 DB 및 역할의 이름/OID를 확인한다.
2. 각 역할이 자신의 DB에 새 TLS 연결을 만들고 `current_user`·`session_user`·
   `current_database`·DB/역할 OID·실제 TLS를 확인한다. 비밀번호는 한 번씩 읽은
   동일 스냅샷을 전후 시험에 사용한다.
3. 다른 앱 역할로 대상 DB에 새 연결을 시도한다.
   접속·TLS·인증은 psycopg/libpq가 수행한다. PostgreSQL 프로토콜을 직접 구현하지 않는다.
4. 실패한 연결에서 소켓이 닫히기 전에 **실제 클라이언트 주소·포트**를 보존한다.
   libpq의 공개 `connect_start` / `connect_poll`을 사용하고 3초 deadline 후 닫는다.
5. 등록된 PostgreSQL의 JSON 감사 로그에서 같은 클라이언트 주소·포트, 사용자·DB,
   시험 시간창에 해당하는 단 하나의 `FATAL / 42501`을 확인한다.
6. 양쪽 정상 접속과 등록 정보를 다시 확인한다.

**왜 application_name만 사용하지 않는가:** 실제 PG18.4에서는 DB CONNECT 권한 거부가
발생할 때 `application_name`이 아직 설정되지 않을 수 있다. 이 필드만 비교하면 실제
거부를 인수할 수 없다. 고유 요청 이름은 유지하되 서버 로그가 이름을 제공하지 않으면
실제 연결 주소·포트·사용자·DB·시간을 대조한다. 이름이 있다면 현재 요청과 일치해야 한다.

`read_audit`는 신뢰된 서버 로그의 출처·전달 완료·조회 범위·권한을 책임진다.
앱 로그나 업로드 JSON을 PostgreSQL 로그라고 받아서는 안 된다. 로그는 최대 2,000행,
행별 64KiB·전체 1MiB로 제한한다. UTC 시각을 사용하며 PG 로그의 밀리초 정밀도를
반영해 시작 시각만 밀리초 경계로 내린다. 호스트 간 시계 동기화가 전제다.

NAT·프록시·Pooler 때문에 서버가 다른 주소·포트를 본다면, 연결을 추정해서 맞추지 않는다.
그 경로는 별도의 검증된 연결 상관관계 수집이 필요하다. 감사 기록이 없거나 중복·충돌하거나
출처/시간이 다르면 UNKNOWN이다. `28P01` 비밀번호 오류를 권한 거부로 세지 않는다.

## 결합 판정과 증거

| 조건 | T5 판정 |
|---|---|
| 양쪽 자원의 전후 허용 대조군 성공 + 객체·DB 모두 거부 | PASS |
| 대조군 성공 + 객체나 DB에서 다른 앱 접근 성공 | FAIL |
| 허용 대조군 실패, 누락된 자원 시험, 신원·등록 불일치 | UNKNOWN |
| 연결 장애·권한 거부 감사 증거 없음·미지원 경로 | UNKNOWN |

두 자원은 같은 대상 앱·다른 앱·환경으로 결속한다. 자원 하나만 PASS여도 전체 PASS가
되지 않는다. 등록 snapshot·run ID·nonce·시각과 증거 저장 성공을 확인한다.
증거에는 고정 결과·신원/자원 해시·서버 거부 영수증 해시만 저장한다.
비밀번호·객체 본문·SQL·서버 오류 원문은 저장하지 않는다.
검사는 시간창 안의 관찰이며 정책이 이후에도 유지된다는 보장은 아니다.

## 검증과 실행

- 단위/SDK 시험: Botocore Stubber의 실제 요청 파라미터·403·만료·객체 버전 변화,
  두 자원 결합, 대조군 실패, 역할 OID, 감사 출처·다른 연결·중복·시각,
  libpq timeout/정리, T5 Runner와 전체 20행 보존.
- 실제 로컬 PG18.4: SCRAM + verify-full, 두 앱 DB/역할로 접속 거부 PASS,
  시험 DB의 CONNECT를 실제 허용하면 FAIL, 잘못된 비밀번호·감사 누락·역할 OID 변화 UNKNOWN.
- **AWS S3 두 역할의 실제 교차 접근과 팀 양쪽 환경은 아직 검증하지 않았다.**

```sh
# 서비스 폴더에서. PostgreSQL 및 Python 시험 이미지 준비만 네트워크 필요.
docker build -t bravo-t14-iso-qualification:20261106 -f testing/app-isolation.Dockerfile .
PYTHONPATH=src python tests/integration/verify_app_isolation_db.py
```

호스트에 Docker·openssl·이 서비스의 aws/postgres 의존성이 필요하다.
시험은 고유 이름/소유권 라벨의 컨테이너와 임시 인증서를 만들고 finally에서 제거한다.
클라이언트는 해당 컨테이너 내부에서 실행해 Docker Desktop의 NAT를 피한다.
DB 준비용 관리자 권한은 시험 하네스에만 있으며 제품 검사에는 전달하지 않는다.
Docker 이미지 및 의존성은 로컬 자격 시험용으로, 운영 배포 승인을 의미하지 않는다.

## 운영 인계

- T6/T9/T30: 대상 앱과 다른 앱의 실제 역할·DB·객체·현재 버전·CA·자원 등록.
- T14 호스트: 기존 등록·단기 자격 공급자, 전용 읽기 감사 조회와 deadline/취소 감독 연결.
- T34: 해당 DB의 실제 JSON 감사 로그 출처 및 전달 완료를 보장하는 읽기 경로.
- T10/T19: 현재 계획·프로필에 결속해 양쪽 ControlRun과 원문/요약 참조 저장·대조.

등록된 앱 한 쌍과 검사한 객체/DB 범위를 표시하며 전체 테넌트 격리 완료로 확장하지 않는다.

근거: [PostgreSQL JSON 로그 필드](https://www.postgresql.org/docs/18/runtime-config-logging.html#RUNTIME-CONFIG-LOGGING-JSONLOG),
[libpq 비동기 접속](https://www.postgresql.org/docs/18/libpq-connect.html),
[psycopg libpq 인터페이스](https://www.psycopg.org/psycopg3/docs/api/pq.html).
