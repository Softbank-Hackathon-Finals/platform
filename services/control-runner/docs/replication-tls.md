# 논리 복제 연결의 TLS 확인 (ENC-01.4)

앱→PgBouncer 검사가 복제 연결의 암호화를 대신하지 않는다. 이 검사기는 등록된
PostgreSQL `pgoutput` 구독 한 개와 그 구독이 사용하는 출발지 복제 슬롯의 실제 연결을
묶어 확인한다. 임의 복제 제품·물리 복제·다중 호스트 연결은 지원하지 않는다.

## 읽기 경로

`PostgreSQLReplicationReader`는 호스트가 준비한 두 개의 전용 psycopg 연결을 사용한다.
연결은 유휴 상태이며 `sslmode=verify-full`, 실제 TLS, 지정한 DB를 만족해야 한다.
앱 풀이나 다른 실행기의 트랜잭션을 재사용하지 않는다.

1. 양쪽 DB의 등록 UID·주소·설정 적용 영수증을 다시 조회한다.
2. 목적지의 `pg_subscription`에서 현재 DB의 지정 구독 1개를 읽는다.
3. 출발지의 지정 `pg_replication_slots.active_pid`를 `pg_stat_replication.pid` 및
   `pg_stat_ssl.pid`에 결합한다. 동일한 슬롯·DB·사용자·application_name·목적지 주소의
   `streaming` 연결을 두 번 확인한다.
4. 구독과 자원 등록을 다시 읽고, 구독 OID·활성 상태·TLS 설정이나 sender PID/시작 시각이
   달라졌으면 UNKNOWN으로 남긴다.

고정된 매개변수 쿼리만 사용하고 각 조회 트랜잭션은 읽기 전용이다. statement timeout은
3초, lock timeout은 1초, 전체 수집 예산은 30초다. 연결 준비·원격 실행의 실제 종료 시한은
호스트가 강제한다. 복제 시작/중단·ALTER SUBSCRIPTION·세션 종료·슬롯 삭제는 하지 않는다.

## 판정 및 비밀 보호

- 구독의 명시적 `sslmode=verify-full`, 등록된 CA 경로와 연결 목적지, 실제 sender의
  TLS 1.2/1.3을 모두 확인하면 `enabled`다.
- 같은 등록 연결에서 `require` 등 약한 설정이나 평문/TLS 구버전을 관측하면 `disabled`다.
- 비활성·누락·다른 슬롯/주소·설정 변경·권한 부족·알 수 없는 프로토콜은 UNKNOWN이다.
- 운영자가 설정을 바꾸기 전에 생성된 연결은 새 설정의 적용을 증명하지 못한다. T8의
  신뢰된 **설정 적용 시각**보다 sender 시작 시각이 이르면 UNKNOWN이다. 수집기가 임의로
  과거 시각을 넣거나 관찰 시각으로 대체하지 않는다. 재접속을 강제로 일으키지도 않는다.

`subconninfo`에는 비밀번호가 들어갈 수 있다. 원문은 등록된 목적지 내부의 신뢰된 수집기에서
메모리로만 파싱하고 반환·로그·증거에 넣지 않는다. 예외 원문도 출력하지 않는다. 공개 결과에는
설정 모드·TLS 여부/버전·결속 성공 여부와 증거 지문만 남긴다. 원천의 조회 권한은 별도 인수하며
이 코드가 관리자 권한을 발급하지 않는다. 실제 업무 연결과 다른 검사 연결의 TLS를 혼동하지 않는다.

## 운영 호스트의 책임

- 실행 전후 DB UID·주소·역할·CA 지문·구독 설정 revision을 인증된 경로로 대조한다.
- `configured_at`은 해당 revision이 적용된 기존 실행 영수증에서 가져온다. 카탈로그 읽기나
  업로드 JSON만으로 이 시각을 증명할 수 없다.
- 등록된 연결에 대한 두 번의 읽기는 분산 원자적 스냅샷이 아니다. 시험 창에서 동시 설정
  변경을 통제한다. 같은 CA 경로의 파일 바꿔치기 등은 호스트의 설정 지문/readback으로 확인한다.
- 인증된 evidence sink에 저장한 원문 참조를 T19에 인계한다. 해시는 원천 인증을 대체하지 않는다.

## 검증과 한계

단위 시험은 설정 불일치·slot/PID 오결속·기존 연결·연결 재시작·평문·권한 거부·증거 저장 실패를
다룬다. `tests/integration/verify_replication_tls.py`는 Docker의 PostgreSQL 두 대에서 실제
논리 복제와 TLS 강등/비활성 경로를 재현한다. 고유 컨테이너·네트워크만 만들고 finally에서 제거한다.
시험 네트워크는 전용 bridge이고 포트는 임의의 localhost 포트에만 공개된다. 폐기할 시험 DB의
trust 인증은 실제 운영 인증이나 네트워크 격리의 검증 근거가 아니다.

```sh
PYTHONPATH=src python tests/integration/verify_replication_tls.py
```

이 로컬 검증은 AWS↔실제 온프렘 운영 구독의 검증이 아니다. 운영 연결에는 T8의 등록·설정 적용
영수증과 양쪽 읽기 접점이 필요하며, 이 검사만으로 복제 지연·데이터 무결성·전환 가능성을 판정하지 않는다.

공식 동작 근거:
[pg_subscription](https://www.postgresql.org/docs/18/catalog-pg-subscription.html),
[pg_replication_slots](https://www.postgresql.org/docs/18/view-pg-replication-slots.html),
[pg_stat_replication·pg_stat_ssl](https://www.postgresql.org/docs/18/monitoring-stats.html).
