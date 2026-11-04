# T14: Pooler → DB 인증서 검증 (ENC-01.5)

`PoolerBackendTLSProbe`는 **실행 중인 PgBouncer 설정과 실제 upstream DB 세션**을 함께 평가한다. 앱 → Pooler의 `PoolerTLSProbe`(ENC-01.1/2/3)와 별도 시험이다. TLS 연결만 있거나 설정 파일에 `verify-full`이 적혔다는 이유로 PASS하지 않는다.

## 수집 경계

이 모듈은 등록된 신뢰 수집기의 결과를 `Observation`으로 만드는 어댑터다. 업로드한 JSON·AI 응답·브라우저가 전달한 자원 ID를 수집기로 등록하면 안 된다. 자동 웹 접수나 팀 EKS/RKE2 등록 경로의 완성을 뜻하지 않는다.

호출자는 서버가 관리하는 앱·환경·Pooler UID·Pod UID·DB Cluster UID·Pod 주소·DB명과 등록 해시를 `BackendTarget`으로 고정한다. 수집기는 다음을 **30초 안에 읽기 전용**으로 수행한다.

1. 해당 Pooler/Pod와 DB의 등록 UID·주소를 재조회하고, Pod→ReplicaSet→Deployment→Pooler의 controller owner UID를 확인한다.
2. 고정 Pooler Pod의 관리 소켓에서 `SHOW CONFIG`를 읽는다. `server_tls_sslmode`는 중복 없이 정확히 하나여야 한다. 파일 내용으로 대체하지 않는다.
3. 바로 그 Pod의 Pooler listener를 통해 앱 DB에 연결한다. 앱 → Pooler도 등록 CA·hostname을 검증한다. 비밀은 명령 인수·로그·보고서에 넣지 않는다.
4. **그 SQL 연결의 `pg_backend_pid()`**에 대해 아래 값을 읽는다. 다른 연결의 TLS 행을 고르면 안 된다.
5. 다시 UID·주소·실행 설정을 조회한다. 전후 binding과 설정이 같아야 한다.

```sql
SELECT json_build_object(
  'backend_started_at', a.backend_start,
  'database', current_database(),
  'client_address', host(inet_client_addr()),
  'server_address', host(inet_server_addr()),
  'ssl', s.ssl,
  'protocol', s.version,
  'pid', pg_backend_pid()
)
FROM pg_stat_ssl s
JOIN pg_stat_activity a USING (pid)
WHERE s.pid = pg_backend_pid();
```

수집기는 시각을 timezone-aware `datetime`으로 변환하고 조회 완료 시각을 붙인다. Postgres `inet::text`는 `/32` 접미사를 포함할 수 있으므로 `host()`를 사용한다. 결과 개수·크기·명령 기한을 제한하고 stdout/stderr 원문을 공개 증거에 저장하지 않는다.

수집 API의 계약은 `collector(context, target, deadline)`이며 `run_id`, `before`, `connection`, `after`를 반환한다. `before/after`에는 `binding`, `collected_at`, `server_tls_sslmode`가 들어간다. `connection`에는 위 SQL 7개 필드와 `collected_at`이 들어간다. 호출자는 deadline을 실제 I/O에서 강제한다. 이 동기 어댑터는 멈춘 임의 Python callback을 강제로 종료하는 샌드박스가 아니다.

## 판정

- `verify-full` + 실제 upstream `ssl=true` + TLS 1.2/1.3 + 등록된 연결/시각 일치: `enabled`.
- 유효한 실제 관측에서 `verify-ca`/`require` 등, 평문 또는 TLS 1.0/1.1: `disabled`.
- 미수집, 알 수 없는 값, UID·주소·DB 불일치, 전후 설정 변화, 오래된 연결, 시각 역전/30초 초과: `UNKNOWN`.
- `backend_start`가 첫 설정 조회보다 이전이면 현재 설정으로 새 handshake를 했다고 증명하지 못하므로 `UNKNOWN`. 시험기는 운영 연결을 끊거나 재시작하지 않는다. 새 세션 확보는 승인된 운영 계획 또는 별도 시험 Pooler에서 수행한다.
- 수집 노드와 DB의 시계가 동기화돼 있어야 한다. 현재 판정은 허용 skew를 임의로 늘리지 않으며 불확실한 시각을 통과시키지 않는다.

증거는 앱·환경·run, binding 해시, 판정·정규화한 TLS 사실만 저장한다. 주소·DB 계정·인증정보·원문 진단은 제외한다. 영속 증거 저장 실패 시 결과를 반환하지 않는다. 해시는 신뢰 수집 주체나 서명을 대신하지 않는다.

## 구현·실측 범위

[로컬 CNPG 실측](../../docs/evidence/t14-pooler-backend-tls-2026-11-03/README.md)에서 분리한 시험 Pooler 두 개를 사용했다. 기존 앱·DB·Pooler는 변경하지 않았다. 앱→Pooler TLS, replication TLS, IAM 권한, 모든 우회 경로의 차단은 이 한 시험으로 증명하지 않는다. 팀 EKS/RKE2 배치는 T6/T30 등록 수집기에 연결한 뒤 같은 시험을 다시 수행해야 한다.
