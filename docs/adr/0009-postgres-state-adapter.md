# ADR-0009 상태·이벤트 저장 계약의 PostgreSQL 어댑터

* **상태 (Status):** 제안됨(Proposed), 로컬 임시 PostgreSQL 검증
* **날짜 (Date):** 2026-11-03
* **관련 (Related):** T10 #19, T7 #12, ADR-0008; 설계 v0.3 §8.2 · §8.9

## 1. 배경 및 문제 상황

ADR-0008의 SQLite 참조 구현은 계획 상태·이벤트·명령 영수증의 원자성과 snapshot watermark를 검증한다. T7 플랫폼 DB에 실제 서비스를 붙이려면 같은 경계를 서버 DB에서도 검증해야 한다. 기존 SSE/조회 계약을 바꾸거나 T33/Executor까지 한 번에 이식하면 검토 범위가 커진다.

## 2. 고려한 옵션들

1. SQLite 로컬 참조만 유지한다. 기존 검증은 남지만 여러 API 프로세스가 붙는 저장소 접점을 검증하지 못한다.
2. 기존 상태·이벤트 코드를 공유하는 PostgreSQL 어댑터 하나를 추가한다. 실제 임시 DB에서 동시성·롤백·재시작·최소 권한을 검증할 수 있다. psycopg와 별도 migration 실행이 필요하다.
3. 새 서비스/ORM·전체 저장소·클라우드 DB를 동시에 도입한다. 운영 선택·다른 모듈 변경이 결합돼 현재 작은 저장 계약 검토 범위를 넘는다.

## 3. 결정 제안

옵션 2를 제안한다. `PostgresPlanStore`는 기존 `PlanStore`의 등록·전이·snapshot·append·read_after 코드를 재사용한다. SQLite는 보존한다. 공개 HTTP/JSON, 두 상태 COLLECT/ANALYZE, tenant와 신뢰된 collector 경계는 변경하지 않는다. 설계 v0.3의 원자 저장 제안을 구현하는 선택형 backend이며 `design.html`의 아키텍처/요구사항을 이번에 변경하지 않는다.

PostgreSQL writer는 plan별 transaction advisory lock을 얻은 뒤 READ COMMITTED로 정본을 읽는다. 최초 등록에도 같은 잠금이 필요하므로 기존 행에만 적용되는 row lock만 사용하지 않는다. counter는 transaction 안에서 갱신하고 실패 시 되돌린다. snapshot/read_after는 REPEATABLE READ READ ONLY로 같은 시점을 본다. [공식 transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html), [advisory lock](https://www.postgresql.org/docs/18/explicit-locking.html#ADVISORY-LOCKS), [psycopg transaction](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)을 따른다.

운영자가 검토할 SQL은 migration/runtime 역할을 분리한다. runtime 어댑터는 DDL 없이 스키마 버전만 확인하고, 오류에 connection data를 노출하지 않는다. 같은 runtime credential의 다른 writer가 직접 SQL을 쓰면 잠금/상태 계약을 우회하므로 모든 상태/event writer가 공통 어댑터를 사용해야 한다. 세부 권한과 실패·재시도 정책은 [저장 계약](../contracts/T10-postgres-storage-v1-proposal.md)에 있다.

## 4. 결과와 제한

- 실제 임시 PostgreSQL의 여러 연결/어댑터에서 원자성·동시성·동일 시점 snapshot·재시도·강제 재시작/WAL 복구·권한 거부를 시험한다. 원래 SQLite 77개 시험을 유지한다.
- API가 검증한 principal/저장된 tenant를 대조하며 DB 계정은 신뢰된 호스트 전용이다. tenant별 RLS나 실제 로그인 구현은 추가하지 않는다.
- 운영 PostgreSQL 엔진/버전, CNPG/RKE2·PVC·백업·HA·PgBouncer·secret 경로의 팀 채택 또는 연결 완료를 의미하지 않는다. 원격 인프라/기반 state는 변경하지 않는다.
- T33 저장소, T12 Executor, 승인·전체 상태 머신, SQLite 데이터 자동 이관·S3 Object Lock은 별도 후속 작업이다.
- ADR-0007을 사용하는 병렬 모델 provider 제안과 역할/문서 번호를 겹치지 않는다. 이 ADR 번호와 저장 계약은 코드 병합만으로 운영 채택 상태로 바뀌지 않는다.
