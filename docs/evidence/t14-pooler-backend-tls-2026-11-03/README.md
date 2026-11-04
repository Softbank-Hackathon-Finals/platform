# T14 ENC-01.5: Pooler → DB 실제 TLS 읽기

2026-11-03, 소유한 로컬 kind 시험 환경에서 CNPG 1.30.1·PostgreSQL 18.4를 사용했다. **팀 EKS/RKE2 운영 검증이 아니다.** 기존 앱→Pooler 검증과 다른 구간이다.

## 결과

- 별도 시험 Pooler 2개를 생성했다. 기본 `verify-ca` 구성과 명시적 `server_tls_sslmode=verify-full` 구성을 비교했다.
- 실행 중인 `SHOW CONFIG`를 SQL 전후에 읽었다. 연결은 각 고정 Pooler Pod의 listener를 통과했고 앱→Pooler도 CA·hostname을 검증했다.
- DB의 해당 `pg_backend_pid()`에서 TLS 1.3·`ssl=true`를 확인했다. client/server 주소를 등록된 Pooler/DB Pod와 대조했다.
- `verify-ca`: ENC-01.5 `disabled` — 암호화는 됐지만 hostname 검증 기준 미충족.
- `verify-full`: ENC-01.5 `enabled` — 설정과 새 upstream 세션을 함께 확인.
- 같은 Pooler에서 기존 backend 세션을 재사용한 경우: `UNKNOWN` — 새 설정으로 handshake했는지 추정하지 않았다.

## 실패와 한계

첫 수집에서는 `inet::text`의 `/32` 접미사를 literal IP로 해석하지 못해 두 결과가 UNKNOWN이었다. SQL을 `host(inet_client_addr())`/`host(inet_server_addr())`로 수정하고 새로 만든 시험 Pooler의 연결만 갱신한 뒤 재검증했다. 판정 조건을 완화하지 않았다.

생산 수집기·서버 등록 연결, 두 실제 환경의 재시험, 운영 트래픽을 건드리지 않는 새 backend 확보, DB/수집기 시계 동기화 확인은 남아 있다. 전후 readback은 임의의 중간 변경을 원자적으로 봉쇄하는 수단이 아니다. 잘못된 CA/hostname에 대한 **upstream 강제 실패** 시험은 이번 증거 범위 밖이며, 기존 frontend 음성 시험으로 대신 주장하지 않는다.

원문 자원 식별자와 실행 기록은 비공개 개발 기록에 보존했다. 공개 JSON은 정규화한 판정과 해시만 포함한다. 기존 자원은 유지하고 새 시험 Pooler는 UID 일치 조건으로만 정리한다.
