# ENC-01.5 실제 Pooler → DB 조회

`PostgreSQLPoolerBackendReader`는 `PoolerBackendTLSProbe`에 고정 SQL 조회를 공급한다.
앱→Pooler TLS 시험과 구분하며, 기존 T5 합격 기준은 바꾸지 않는다.

## 호스트가 제공할 것

- 인증된 서버 등록에서 앱·환경, Pooler/Pod/DB UID, DB 이름과 양쪽 IP를 결속한다.
  `read_registration`은 같은 등록을 조회 전후에 다시 읽는다. 업로드 JSON을 신뢰하는 콜백이 아니다.
- 등록된 고정 listener에 `host`·`hostaddr`·포트와 CA를 지정한 `verify-full` 연결을 사용한다.
  통계 계정은 `stats_users`에 속하며 `pgbouncer` DB에서 `SHOW CONFIG`를 읽을 수 있어야 한다.
  관리자 계정을 요구하지 않는다. 통계 연결은 전용·idle·autocommit이어야 한다.
- 앱 연결 factory는 별도의 새 psycopg 연결을 만든다. 공유 앱 풀의 연결을 빌려주지 않는다.
  앱 역할·DB와 등록 주소를 검사하고, 조회가 끝나면 그 연결만 닫는다.
- 전체 실행을 30초 이내에 종료하는 호스트 worker와 연결 timeout을 설정한다.
  SQL의 3초 timeout만으로 멈춘 통계 소켓이나 연결 factory를 강제 종료할 수는 없다.
- 원문 증거를 영속 저장하는 sink를 연결한다. 저장 실패를 PASS로 반환하지 않는다.

직접 고정 listener가 첫 지원 경로다. 임의 localhost 포트포워드를 등록된 Pod와 같은 것으로
간주하지 않는다. 터널을 사용하려면 별도의 신뢰된 전송·자원 결속을 먼저 인수해야 한다.
자격·CA 개인 키·원문 SQL 오류를 결과나 공개 문서에 넣지 않는다.

## 실제 조회 순서

1. 현재 등록과 통계 연결의 TLS·주소·계정을 검증한다.
2. `SHOW CONFIG`의 실제 `server_tls_sslmode`를 읽는다.
3. 전용 앱 연결을 열고 READ ONLY 트랜잭션에서 현재 backend의 TLS·PID·시작 시각·DB·양쪽 IP를 읽는다.
4. 같은 트랜잭션을 유지한 채 `SHOW CONFIG`를 다시 읽고 등록을 재확인한다.
5. 기존 판정기가 전후 설정·실제 TLS·새 backend·시간 범위를 검사한다.

`inet::text` 대신 `host(inet_client_addr())`/`host(inet_server_addr())`를 사용해
주소의 `/32` 표현 차이를 제거한다. 사용자 SQL·쓰기·`PAUSE`·`RECONNECT`·reload·세션 종료는 실행하지 않는다.
설정 조회 전에 생긴 backend를 재사용하면 UNKNOWN으로 남긴다. 시험 통과를 위해 운영 풀을 비우지 않는다.
설정 전후 조회는 중간 변경을 원자적으로 막는 수단이 아니며, 운영 변경 창과 시계 동기화가 필요하다.

## 재현

Docker 환경에서 저장소의 회전 시험 이미지와 고정 PgBouncer 이미지를 준비한다.
회전 이미지 빌드 방법은 [비밀 회전 시험](secret-rotation.md)을 따른다. 이 시험은 앱 회전을 실행하지 않는다.

```sh
cd services/control-runner
uv sync --locked
PYTHONPATH=src uv run --locked python tests/integration/verify_pooler_backend_reader.py
```

스크립트는 network none인 PostgreSQL과 그 네트워크 공간만 공유하는 PgBouncer를 생성한다.
포트를 호스트에 공개하지 않고 비 root·capability 제거 조건으로 실행한다.
자신의 실행 label을 가진 컨테이너만 정리하며, 정리 실패도 성공 출력으로 숨기지 않는다.

[2026-11-06 실제 격리 시험 결과](../../../docs/evidence/t14-pooler-reader-2026-11-06/README.md)는
수집기 자격 근거다. 팀 출발지·온프렘 목적지의 운영 인수나 전체 T14 PASS는 아니다.
