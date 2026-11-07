# T13 DB 안 무결성 지문 v1

쓰기를 막은 뒤 두 DB가 같은지 확인하는 규칙이다. 각 DB가 자기 안에서 지문(해시)을 계산하고, Executor는 지문과 카탈로그 정보만 받아 비교한다. 행은 네트워크로 나오지 않는다.

- 코드: `services/cutover-executor/src/bravo_cutover/db_fingerprint.py`
- 시험: `services/cutover-executor/tests/test_db_fingerprint.py`, `test_db_fingerprint_runner.py`
- 측정: [증거 폴더](../evidence/t13-db-fingerprint-2026-11-04/README.md)
- 계산 위치를 정한 이유와 검토 기준: ADR-0013 ([#145](https://github.com/Softbank-Hackathon-Finals/platform/pull/145)에서 제안)

대상은 지금 데모의 `messages`, `attachments`다. 첨부 원본 파일의 바이트는 비교하지 않는다.

## 규칙 값

```text
db-sql-v1:fixed-session-text;columns-by-name;jsonb-array;utf8;sha256-of-row-sha256;pk-order-collate-c
```

규칙이 바뀌면 이 값을 올린다.

## 지문을 만드는 순서

1. **한 시점만 본다.** 표 하나마다 `REPEATABLE READ, READ ONLY` 트랜잭션을 연다.
2. **세션을 고정한다.** 트랜잭션 안에서만 적용되고 끝나면 사라진다.

   | 설정 | 값 | 이유 |
   |---|---|---|
   | `statement_timeout` | 3000 | 쿼리 하나가 3초를 넘으면 서버가 끊는다. 이 구현의 적용값이고, 근거는 로컬 측정뿐이다 |
   | `search_path` | `pg_catalog` | 다른 스키마의 같은 이름 함수 · 타입을 쓰지 않는다 |
   | `row_security` | `off` | 행 보안 정책이 행을 숨기면 조용히 빠지는 대신 오류가 난다 |
   | `TimeZone` | `UTC` | 같은 시각이 같은 글자로 나온다 |
   | `DateStyle` | `ISO, YMD` | 날짜 표기를 고정한다 |
   | `IntervalStyle` | `postgres` | 기간 표기를 고정한다 |
   | `extra_float_digits` | `1` | 실수를 잘림 없이 가장 짧은 정확한 글자로 쓴다 |
   | `bytea_output` | `hex` | 바이너리 표기를 고정한다 |

3. **행을 글자로 바꾼다.** `jsonb_build_array(컬럼::text, ...)::text`. 컬럼은 이름순이다. NULL은 JSON `null`, 글자 `NULL`은 `"NULL"`이라 구분되고, 값 경계가 섞이지 않는다.
4. **행 지문을 만든다.** `sha256(convert_to(위 글자, 'UTF8'))`.
5. **표 지문을 만든다.** 행 지문을 PK 순서로 이어 붙여 다시 `sha256`한다. 글자 PK는 `COLLATE "C"`(바이트 순서)로 정렬해 DB 정렬 설정의 영향을 받지 않는다. PK 지문은 PK 컬럼만, 내용 지문은 전체 컬럼으로 만든다.

## 지원 버전

양쪽 PostgreSQL 메이저가 같고, 그 메이저가 16 또는 18일 때만 판정한다. 16은 CI에서 도는 버전이고 18은 실제 DB 버전이다.

- **메이저가 다르면** 지문이 같아도 달라도 `UNKNOWN`이다. 값 표현과 카탈로그가 메이저마다 다를 수 있고, 호환을 검증하지 않았기 때문이다.
- **마이너만 다르면** 그대로 판정한다. 메이저 번호만 비교한다.
- **16 · 18이 아닌 메이저는** `UNKNOWN`이다. 코드가 PG17의 카탈로그 칸도 읽지만 시험을 돌리지 않았으므로 허용하지 않는다. 허용하려면 그 버전으로 이 시험을 돌린 뒤 `SUPPORTED_MAJORS`에 더한다.

## 지원 타입

아래 타입만 계산한다. 이 밖의 타입(`money`, 배열, enum, 도메인, 복합, 기하 등)이 있으면 그 표는 `UNKNOWN`이다.

`boolean` `smallint` `integer` `bigint` `numeric` `real` `double precision` `text` `character varying` `character` `bytea` `uuid` `date` `time` `timetz` `timestamp` `timestamptz` `interval` `json` `jsonb`

`relkind`가 일반 표가 아니거나 상속 자식이 있는 표, PK가 없는 표, 스키마 이름이 없는 표 이름도 `UNKNOWN`이다. `jsonb_build_array`의 인자 제한 때문에 컬럼이 100개를 넘는 표도 `UNKNOWN`(SQLSTATE `54023`)이다.

## 스키마 지문

카탈로그만 읽어 Executor에서 해시한다. 행은 보지 않는다. 정렬 규칙 검사는 [#134](https://github.com/Softbank-Hackathon-Finals/platform/pull/134) `98a9fec`와 같다.

- `schema_definition_hash`: DB 정렬 설정(인코딩 · 제공자 · `datcollate` · `datctype` · 로케일 · 규칙), 컬럼(순서 · 이름 · 타입 · NOT NULL · identity · generated · 기본값 · 정렬 의미), 제약(종류와 정의), PK.
- `schema_hash`: 위 정의 + PostgreSQL 메이저 + 정렬 제공자 버전. 게이트처럼 이 값 하나만 비교해도 버전 차이에서 통과하지 않는다.

해시에 넣는 순서는 DB 설정에 영향을 받지 않는다. 컬럼은 물리 순서, 제약은 Python 정렬(코드포인트 순서, `COLLATE "C"`와 같은 순서)이다.

정렬 규칙은 이름 · OID가 아니라 제공자 · deterministic · 인코딩 · 로케일 · 규칙으로 비교한다. 컬럼의 기본 정렬은 DB 설정으로 풀어 쓴다. 제약 이름은 비교하지 않는다. 기록된 버전과 실제 제공자 버전이 다르거나 제공자를 모르면 `UNKNOWN`이다.

## 판정

표마다 데이터 판정과 스키마 판정을 따로 낸다. 진행은 `verdict`가 `MATCH`일 때만이다.

| 판정 | 조건 | 값 |
|---|---|---|
| 데이터 | `rows` · `pk_hash` · `content_hash`가 모두 같음 | `MATCH` |
| 데이터 | 하나라도 다름 | `MISMATCH` |
| 스키마 | `schema_hash`가 같음 | `MATCH` |
| 스키마 | `schema_hash`는 다르고 `schema_definition_hash`는 같음(제공자 버전만 다름) | `UNKNOWN` |
| 스키마 | 둘 다 다름 | `MISMATCH` |
| 둘 다 | 양쪽 PostgreSQL 메이저가 다름 | `UNKNOWN` |
| 둘 다 | 미지원 타입 · 미지원 메이저 · 시간 초과 · 연결 실패 · 오래된 정렬 버전 등 지문을 못 읽음 | `UNKNOWN` |

전체 값은 하나라도 `MISMATCH`면 `MISMATCH`, 그렇지 않고 모두 `MATCH`면 `MATCH`, 나머지는 `UNKNOWN`이다. 비교한 표가 없어도 `UNKNOWN`이다.
### 오류 코드

표 결과의 `error`에는 아래 고정 코드만 들어가고, `sqlstate`에는 형식이 맞는(영문 대문자 · 숫자 5자) SQLSTATE만 들어간다. 연결 문자열 · 호스트 · 서버 오류 원문 · 표와 컬럼 이름은 복사하지 않는다. 원문은 예외 연결(cause)로도 넘기지 않아 로그에 찍히지 않는다.

| 코드 | 뜻 |
|---|---|
| `connection_failed` | 연결 실패. 다른 모듈이 던진 불명도 원문을 버리고 이 코드가 된다 |
| `statement_timeout` | 쿼리 하나가 3초를 넘음 |
| `snapshot_unreadable` | 그 밖의 DB 오류. 원인은 `sqlstate`로 본다 |
| `table_not_qualified` · `table_missing` · `not_plain_table` · `primary_key_missing` | 비교할 수 없는 표 |
| `unsupported_type` · `unsupported_major` · `collation_unverified` | 지원하지 않는 타입 · 메이저, 낡거나 모르는 정렬 규칙 |
| `major_version_mismatch` | 양쪽 메이저가 다름. 양쪽 값은 `source` · `target`의 `server_major`에 있다 |

어느 컬럼이 미지원인지는 영수증에 없다. 지원 타입 목록과 카탈로그를 대조해 찾는다.

## 시퀀스

쓰기를 막은 뒤 `correct_sequence(src, dst, 시퀀스, 표, 컬럼)`을 부른다. 올리기만 한다.

```text
다음 값 = last_value + 1 (is_called가 true), last_value (false)
올릴 값 = max(출발지 다음 값, 출발지 최대 ID + 1, 목적지 다음 값, 목적지 최대 ID + 1)
```

- 값을 읽을 때도 지문과 같은 3초 제한과 `row_security=off`를 건다. 행 보안 정책이 행을 숨겨 최대 ID가 작게 읽히면 조용히 넘어가지 않고 오류가 난다.
- 목적지 다음 값이 이미 올릴 값이면 쓰기를 보내지 않는다.
- 올린 뒤 다시 읽어 다음 값이 올릴 값과 다르면 `Unconfirmed`(`sequence_readback_mismatch`)다.
- 증가폭 1이 아니거나 순환하는 시퀀스, 올릴 값이 시퀀스 최댓값을 넘는 경우도 `Unconfirmed`(`sequence_unsupported`, `sequence_over_maximum`)이고 값을 바꾸지 않는다. 이 함수가 올리는 예외도 고정 코드와 SQLSTATE만 담는다. 연결 실패는 `connection_failed`, 시퀀스를 올리는 호출의 실패는 `sequence_unconfirmed`다.
- 노션 초안의 여유값 1000은 넣지 않았다(ADR-0013 결정 4).

## 결과 모양

`fingerprint(conn, 표)`:

```json
{"rows": 3, "schema_hash": "…", "schema_definition_hash": "…", "server_major": 18, "pk_hash": "…", "content_hash": "…"}
```

`DbFingerprintComparer().compare(src, dst, 표 목록)`: #134 `compare()`의 칸에 판정 칸만 더했다.

```json
{"hash_rule": "db-sql-v1:…", "verdict": "MATCH", "data_verdict": "MATCH", "schema_verdict": "MATCH",
 "tables": [{"name": "public.messages", "rows_src": 3, "rows_dst": 3, "source": {}, "target": {},
             "match": true, "data_verdict": "MATCH", "schema_verdict": "MATCH"}]}
```

못 읽은 표는 `rows_src` · `rows_dst` · `source` · `target`이 `null`이고 `error` · `sqlstate`가 붙는다. 메이저가 다른 표는 읽은 지문을 남기고 `error`가 `major_version_mismatch`다.

`correct_sequence()`의 영수증 항목: `name` `src_last` `src_is_called` `src_next` `src_max_id` `dst_last_before` `dst_is_called_before` `dst_next_before` `dst_max_id` `dst_set` `raised`. 설계 8.6의 `src_last` · `dst_set`을 포함한다.

## #134 Python 지문과 다른 점

같은 데이터의 지문 값이 서로 다르고 판정도 다를 수 있어 `hash_rule`을 나눴다.

| 항목 | #134 `v1:pg-types-collation;…` | 이 규칙 `db-sql-v1:…` |
|---|---|---|
| 계산 위치 | Executor가 행을 읽어서 | 각 DB 안에서 |
| `numeric` 1.5와 1.50 | 같음 | 다름 |
| `jsonb` 1과 1.0 | 같음 | 다름 |
| 내용 지문의 컬럼 순서 | 물리 순서 | 이름순 |
| 미지원 타입 판단 | Python으로 읽은 값의 타입 | DB 컬럼 타입(허용 목록) |
| 판정 | `MATCH` · `MISMATCH` 하나 | 데이터 · 스키마 분리, `UNKNOWN` 값 |

데모 표(`messages` · `attachments`)에는 `numeric` · `jsonb`가 없어 위 표기 차이가 나타나지 않는다. 그런 컬럼이 생기면 양쪽 표기가 같아야 `MATCH`다. 논리 복제는 값을 그대로 옮기므로 표기가 달라지면 조사할 대상으로 본다.

## 소비자 연결

**T12 (`runner.py`).** [#176](https://github.com/Softbank-Hackathon-Finals/platform/pull/176)에서 연결했다. 컷오버 5단계 `_compare`가 아래처럼 쓴다. 실제 비교 결과가 실행 단계 판정을 통과하는지는 `tests/test_db_fingerprint_runner.py`가, 시퀀스 순서와 소유권은 `tests/test_runner_fakes.py`가 잰다.

1. 비교기는 `DbFingerprintComparer()` 하나다. `integrity.py`, `RowCountComparer`, `allow_unverified_compare`는 지웠다.
2. 시퀀스는 판정 전이 아니라 비교가 MATCH일 때만(`_not_matched`가 `None`일 때) `correct_sequence`로 올린다. 중단될 실행에서는 목적지에 쓰지 않는다. 소유권은 시퀀스를 읽기 전이 아니라 실제 `raise_sequence` 직전에 다시 확인한다. `_GuardedRaise`가 읽기는 그대로 넘기고 이 호출만 `_act`로 보내므로, 읽는 도중 넘겨받혔으면 올리지 않고 `OwnershipLost`로 멈춘다. 확인과 호출 사이의 짧은 틈은 남는다(ADR-0011).
3. 영수증에 `data_verdict` · `schema_verdict`와 `sequences`를 남기고 목적지를 열기 전에 저장한다. 비교가 MATCH가 아니면 `sequences`는 비어 있다. 시퀀스 보정을 확인하지 못하면 `sequence_error`(`code`, `sqlstate`)를 남기고 쓰기 전 실패로 끝낸다.
4. 진행 조건은 `_not_matched`의 허용 목록이다. 전체 · 표별 데이터 · 스키마 판정, `match`, 양쪽 지문, `hash_rule`이 이 문서의 「결과 모양」과 맞고 표가 빠지거나 겹치지 않을 때만 진행한다.

**T19 (데이터 준비, [#141](https://github.com/Softbank-Hackathon-Finals/platform/pull/141)).** SQL을 다시 만들지 않고 아래 함수를 읽어 게이트 입력을 채운다. `services/control-gate`는 이 패키지에 의존하지 않으므로, 함수를 어디서 호출할지는 T19 생산자에서 정한다.

| 게이트 입력 | T13 값 |
|---|---|
| `schema_rule` | `HASH_RULE` |
| `tables[].schema_hash`, `primary_key` | `schema_fingerprint(conn, 표)` |
| `source_next`, `target_next`, `target_max_id` | `sequence_state()`의 `next_value`, `max_id` |
| `proposed_next` | `next_safe_value(출발지, 목적지)`. 쓰지 않고 읽기만 한다 |
| `readback_next` | 리허설에서 `correct_sequence()`가 돌려준 `dst_set` |
| `increment`, `cycle` | `1`, `false`. 다르면 `sequence_state()`가 `Unconfirmed`를 던진다 |

실제 환경의 생산자는 Executor `POST /v1/readiness`다(T19, `bravo_cutover/readiness.py`). 위 표의 함수를 실행기와 같은 제어 연결로 부르고, 목적지가 닫혀 있을 때만 `correct_sequence`로 올린 값을 `readback_next`로 낸다. 게이트 쪽 묶음은 `bravo_gate.t13.snapshot_from_observation`이다.

게이트는 `schema_hash`만 비교하므로 제공자 버전이나 메이저만 다른 경우도 `SCHEMA_MISMATCH`로 막힌다. 최종 비교의 `UNKNOWN`과 값은 다르지만 둘 다 진행하지 않는다.

**T20 (되돌리기).** 같은 함수에 인자를 바꿔 넘긴다. `compare(목적지, 출발지, 표)`, `correct_sequence(목적지, 출발지, 시퀀스, 표, 컬럼)`. 결과의 `rows_src` · `src_*`는 지금 쓰고 있는 쪽을 뜻한다.

## 계약 사례

모두 일회용 PostgreSQL에서 실제로 돈다. 괄호는 `test_db_fingerprint.py`의 시험 이름 일부다.

| 사례 | 데이터 | 스키마 | 전체 |
|---|---|---|---|
| 같은 데이터, 입력 순서만 다름 (`same_data`) | MATCH | MATCH | MATCH |
| 빈 표 (`empty_tables`) | MATCH | MATCH | MATCH |
| 복합 글자 PK, DB 정렬만 다름 (`composite_text_key`) | MATCH | | |
| 값 변경 · 글자 NULL과 NULL · 1µs · bytea · float (`changed_value`) | MISMATCH | MATCH | MISMATCH |
| `numeric` 1.5→1.50, `jsonb` 1→1.0 (`changed_value`) | MISMATCH | MATCH | MISMATCH |
| 행 삭제 (`deleted_row`) | MISMATCH | MATCH | MISMATCH |
| 값 경계 이동 (`moved_value_boundary`) | 지문이 다름 | | |
| 컬럼 추가 (`added_column`) | MISMATCH | MISMATCH | MISMATCH |
| 기본값 · 제약 · 타입 · NOT NULL만 변경 (`schema_only_change`) | MATCH | MISMATCH | MISMATCH |
| identity 종류만 변경 (`identity_kind`) | MATCH | MISMATCH | MISMATCH |
| DB 인코딩만 다름 (`database_encoding`) | MATCH | MISMATCH | MISMATCH |
| DB 기본 정렬 · DB ICU · 컬럼 정렬만 다름 (`database_collation`, `column_collation`) | MATCH | MISMATCH | MISMATCH |
| 이름만 다른 같은 정렬 (`equivalent_collations`) | MATCH | MATCH | MATCH |
| 시간대 · 표시 설정만 다름 (`session_setting_only`) | MATCH | MATCH | MATCH |
| 정렬 버전이 낡음 (`stale_*`) | UNKNOWN | UNKNOWN | UNKNOWN |
| 양쪽 정의는 같고 제공자 버전만 다름 (`verdict_table`, 증거의 실제 서버 조합) | MATCH | UNKNOWN | UNKNOWN |
| 양쪽 메이저가 다름, PG16과 PG18 실제 서버 (`different_major_versions`) | UNKNOWN | UNKNOWN | UNKNOWN |
| 시험하지 않은 메이저 (`major_outside`) | UNKNOWN | UNKNOWN | UNKNOWN |
| 마이너만 다름, 18.3과 18.4 실제 서버 (증거) | MATCH | MATCH | MATCH |
| 미지원 타입 (`unsupported_type`) | UNKNOWN | UNKNOWN | UNKNOWN |
| 연결 실패, 원문에 민감값 포함 (`bad_connection_text`, `raw_exception`, `connection_error_text`) | UNKNOWN | UNKNOWN | UNKNOWN |
| 3초 초과 · 잠금 대기 초과 (`statement_timeout`, `timeout_through_compare`) | UNKNOWN | UNKNOWN | UNKNOWN |
| PK 없음 · 표 없음 · 뷰 · 상속 (`unverifiable_table`) | UNKNOWN | UNKNOWN | UNKNOWN |

시퀀스 사례: 최대 ID보다 뒤처진 시퀀스, `is_called=false`, 이미 앞선 목적지, 다시 실행, 미지원 시퀀스, 최댓값 초과, 올리기가 반영되지 않은 경우.

되돌리기 사례(`failback`): 방향을 바꾼 비교와 시퀀스 보정, 역복제가 안 된 행 검출.

## 확인하지 않은 것

- 실제 Tailscale · TLS 경로와 RKE2 위 CNPG에서의 시간. 측정값은 모두 로컬이다.
- 부하 뒤 실제 데이터에서 쿼리 하나가 3초 안에 끝나는지.
- `DbControl`의 기존 메서드(`_connect`, `_one`, `_do` 등)는 지금도 연결 · 서버 원문을 `Unconfirmed`에 담고, `runner._describe`가 이를 실행 결과에 저장한다. 이 문서의 함수를 거치는 경로만 고정 코드로 줄였다.
- 실제 환경의 컷오버. 실행 단계 연결은 #176에서 끝났고, #176 기록에는 k3d 모의 환경의 정상 컷오버 영수증이 `MATCH` · `db-sql-v1`로 남았다고 되어 있다. 실제 복제 환경(EKS · RKE2)에서 이 비교기로 끝까지 돈 적은 없다.
- 시퀀스가 여러 개일 때 일부만 올라간 경우. 지금 설정은 `public.messages_id_seq` 하나다.
