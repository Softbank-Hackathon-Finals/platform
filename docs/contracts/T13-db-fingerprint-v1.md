# T13 DB 안 무결성 지문 v1

쓰기를 막은 뒤 두 DB가 같은지 확인하는 규칙이다. 각 DB가 자기 안에서 지문(해시)을 계산하고, Executor는 지문과 카탈로그 정보만 받아 비교한다. 행은 네트워크로 나오지 않는다.

- 코드: `services/cutover-executor/src/bravo_cutover/db_fingerprint.py`
- 시험: `services/cutover-executor/tests/test_db_fingerprint.py`
- 측정: [증거 폴더](../evidence/t13-db-fingerprint-2026-11-04/README.md)
- 계산 위치를 정한 이유: ADR-0013 ([#145](https://github.com/Softbank-Hackathon-Finals/platform/pull/145)에서 제안)

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
   | `statement_timeout` | 3000 | 쿼리 하나가 3초를 넘으면 서버가 끊는다 |
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

## 지원 타입

아래 타입만 계산한다. 이 밖의 타입(`money`, 배열, enum, 도메인, 복합, 기하 등)이 있으면 그 표는 `UNKNOWN`이다.

`boolean` `smallint` `integer` `bigint` `numeric` `real` `double precision` `text` `character varying` `character` `bytea` `uuid` `date` `time` `timetz` `timestamp` `timestamptz` `interval` `json` `jsonb`

`relkind`가 일반 표가 아니거나 상속 자식이 있는 표, PK가 없는 표, 스키마 이름이 없는 표 이름도 `UNKNOWN`이다.

## 스키마 지문

카탈로그만 읽어 Executor에서 해시한다. 행은 보지 않는다. 정렬 규칙 검사는 [#134](https://github.com/Softbank-Hackathon-Finals/platform/pull/134) `98a9fec`와 같다.

- `schema_definition_hash`: DB 정렬 설정(인코딩 · 제공자 · `datcollate` · `datctype` · 로케일 · 규칙), 컬럼(순서 · 이름 · 타입 · NOT NULL · identity · generated · 기본값 · 정렬 의미), 제약(종류와 정의), PK.
- `schema_hash`: 위 정의 + 정렬 제공자 버전. 게이트처럼 이 값 하나만 비교해도 버전 차이에서 통과하지 않는다.

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
| 둘 다 | 미지원 타입 · 시간 초과 · 연결 실패 · 오래된 정렬 버전 등 지문을 못 읽음 | `UNKNOWN` |

전체 값은 하나라도 `MISMATCH`면 `MISMATCH`, 그렇지 않고 모두 `MATCH`면 `MATCH`, 나머지는 `UNKNOWN`이다. 비교한 표가 없어도 `UNKNOWN`이다. 오류 내용에 행 값이 섞이지 않도록 DB 오류는 SQLSTATE만 남긴다.

## 시퀀스

쓰기를 막은 뒤 `correct_sequence(src, dst, 시퀀스, 표, 컬럼)`을 부른다. 올리기만 한다.

```text
다음 값 = last_value + 1 (is_called가 true), last_value (false)
올릴 값 = max(출발지 다음 값, 출발지 최대 ID + 1, 목적지 다음 값, 목적지 최대 ID + 1)
```

- 목적지 다음 값이 이미 올릴 값이면 쓰기를 보내지 않는다.
- 올린 뒤 다시 읽어 다음 값이 올릴 값과 다르면 `Unconfirmed`다.
- 증가폭 1이 아니거나 순환하는 시퀀스, 올릴 값이 시퀀스 최댓값을 넘는 경우도 `Unconfirmed`이고 값을 바꾸지 않는다.
- 노션 초안의 여유값 1000은 넣지 않았다(ADR-0013 결정 4).

## 결과 모양

`fingerprint(conn, 표)`:

```json
{"rows": 3, "schema_hash": "…", "schema_definition_hash": "…", "pk_hash": "…", "content_hash": "…"}
```

`DbFingerprintComparer().compare(src, dst, 표 목록)`: #134 `compare()`의 칸에 판정 칸만 더했다.

```json
{"hash_rule": "db-sql-v1:…", "verdict": "MATCH", "data_verdict": "MATCH", "schema_verdict": "MATCH",
 "tables": [{"name": "public.messages", "rows_src": 3, "rows_dst": 3, "source": {}, "target": {},
             "match": true, "data_verdict": "MATCH", "schema_verdict": "MATCH"}]}
```

못 읽은 표는 `rows_src` · `rows_dst` · `source` · `target`이 `null`이고 `error`가 붙는다.

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

**T12 (`runner.py`).** 기존 비교기 자리에 넣는다. 실제 비교 결과가 실행 단계 판정을 통과하는지는 `tests/test_db_fingerprint_runner.py`가 재고 있다.

1. `IntegrityComparer()`를 `DbFingerprintComparer()`로 바꾼다.
2. `_compare`의 시퀀스 계산을 `self._act(run, correct_sequence, src.db, dst.db, name, table, column)`로 바꾼다.
3. 영수증에 `data_verdict` · `schema_verdict`를 더한다.
4. `integrity.py`, `DbControl.fingerprint`, `test_integrity.py`를 지우고 README의 「T13 비교 규칙 v1」을 이 문서 링크로 바꾼다. 시험용 `FakeDb`에는 `sequence_state`를 더한다.

**T19 (데이터 준비).** `schema_fingerprint(conn, 표)`가 `schema_hash` · `primary_key`를 준다. 시퀀스는 쓰지 않고 `sequence_state`와 `next_safe_value`로 읽어 제안값을 만든다. 이 패키지를 어떻게 호출할지는 T19 생산자에서 정한다.

**T20 (되돌리기).** 같은 함수에 출발지와 목적지를 바꿔 넘긴다.

## 계약 사례

모두 일회용 PostgreSQL에서 실제로 돈다. 괄호는 `test_db_fingerprint.py`의 시험 이름 일부다.

| 사례 | 데이터 | 스키마 | 전체 |
|---|---|---|---|
| 같은 데이터, 입력 순서만 다름 (`same_data`) | MATCH | MATCH | MATCH |
| 값 변경 · 글자 NULL과 NULL · 1µs · bytea · float (`changed_value`) | MISMATCH | MATCH | MISMATCH |
| `numeric` 1.5→1.50, `jsonb` 1→1.0 (`changed_value`) | MISMATCH | MATCH | MISMATCH |
| 행 삭제 (`deleted_row`) | MISMATCH | MATCH | MISMATCH |
| 값 경계 이동 (`moved_value_boundary`) | 지문이 다름 | | |
| 컬럼 추가 (`added_column`) | MISMATCH | MISMATCH | MISMATCH |
| 기본값 · 제약 · 타입만 변경 (`schema_only_change`) | MATCH | MISMATCH | MISMATCH |
| DB 기본 정렬 · DB ICU · 컬럼 정렬만 다름 (`database_collation`, `column_collation`) | MATCH | MISMATCH | MISMATCH |
| 이름만 다른 같은 정렬 (`equivalent_collations`) | MATCH | MATCH | MATCH |
| 시간대 · 표시 설정만 다름 (`session_setting_only`) | MATCH | MATCH | MATCH |
| 정렬 버전이 낡음 (`stale_*`) | UNKNOWN | UNKNOWN | UNKNOWN |
| 양쪽 정의는 같고 제공자 버전만 다름 (`verdict_table`, 증거의 실제 서버 조합) | MATCH | UNKNOWN | UNKNOWN |
| 미지원 타입 (`unsupported_type`) | UNKNOWN | UNKNOWN | UNKNOWN |
| 3초 초과 · 잠금 대기 초과 (`statement_timeout`, `timeout_through_compare`) | UNKNOWN | UNKNOWN | UNKNOWN |
| PK 없음 · 표 없음 · 뷰 · 상속 (`unverifiable_table`) | UNKNOWN | UNKNOWN | UNKNOWN |

시퀀스 사례: 최대 ID보다 뒤처진 시퀀스, `is_called=false`, 이미 앞선 목적지, 다시 실행, 미지원 시퀀스, 최댓값 초과, 올리기가 반영되지 않은 경우.

## 확인하지 않은 것

- 실제 Tailscale · TLS 경로와 RKE2 위 CNPG에서의 시간. 측정값은 모두 로컬이다.
- 부하 뒤 실제 데이터에서 쿼리 하나가 3초 안에 끝나는지.
- Executor 실행 단계에 연결한 상태의 컷오버. 이 문서의 코드만으로 컷오버가 완료되지 않는다.
