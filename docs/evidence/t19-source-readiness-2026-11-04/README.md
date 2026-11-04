# T19 실제 출발지 스키마 수집과 미완료 입력 차단

2026-11-04 19:10 KST에 출발지 앱의 기존 실행 환경에서 변경 없는 T19 `collect_schema`와
T13 DB 내부 스키마 지문 코드를 실행했다. **출발지 읽기 검증이며 전체 게이트 인수가 아니다.**

| 입력 | 실제 확인 | 결과 |
|---|---|---|
| 실제 출발지 DB | `public.messages`, `public.attachments`의 열·PK·스키마 지문 수집 | 두 테이블 수집 성공 |
| 같은 실제 DB, 다른 기대 DB 이름 | 실제 연결 DB와 등록 기대값 대조 | 두 테이블 모두 UNKNOWN (`SCHEMA_CAPTURE_UNAVAILABLE`) |
| 미연결 목적지 | 연결 불가 입력을 명시적으로 반환 | 스키마·PK 미확인 |
| 출발지 결과 + 미연결 목적지 + 복제·시퀀스 증거 없음 | 기존 `assess_readiness` 실행 | BLOCK |

`result.json`은 제품 출력과 시험 범위를 묶은 결과다. source/target 참조와 계획은 이 운영자
시험의 별칭이다. T10에 등록된 실제 계획 또는 승인 권한으로 취급하지 않는다. 목적지 연결 실패는
실제 목적지 장애를 일으킨 것이 아니라, 미연결 상태를 전달한 음성 입력이다.

## 실행 경계

- 기존 앱의 IRSA로 앱 DB Secret을 메모리에서 읽었다. 운영자 자격을 앱에 주입하거나 DB 자격을
  출력·파일로 반출하지 않았다. PostgreSQL은 `verify-full`과 기존 CA로 접속했다.
- DB 연결은 읽기 전용·autocommit으로 열고 statement 3초, lock 1초 한도를 적용했다.
  DDL·DML·시퀀스 보정·쓰기 전환·복제 변경을 실행하지 않았다.
- 제품 파일은 `source-manifest.json`에 revision과 SHA-256을 기록했다. 임시 디렉터리의 별도
  Python 프로세스에서 실행했으며 앱 설치나 Deployment를 변경하지 않았다.
- 첫 시도는 date-time 검사 의존성 부재로 DB Secret 읽기 전에 실패했다. lockfile의 wheel
  SHA-256을 확인한 `rfc3339-validator`와 `six`를 번들에 보충해 재실행했다.
  초기 실패는 비공개 기록으로 보존하고 `summary.json`에 해시를 남겼다.
- 기반 JSON Schema 의존성 번들은 T14 실환경 시험과 동일하다.
  [T14 의존성 기록](https://github.com/Softbank-Hackathon-Finals/platform/blob/5712002b6efea6207b188e2e3165a0ac30df9abd/docs/evidence/t14-qualified-aws-2026-11-04/dependency-hashes.json)과
  `additional-dependencies.json`을 함께 읽는다. 원격 프로세스 전체 한도는 120초다.

## 해석과 남은 인수

`freshness` PASS는 그 입력 객체를 방금 만들었다는 검사일 뿐, 목적지 연결이나 복제 성공을
뜻하지 않는다. 최종 결과는 누락된 목적지 스키마·PK·복제 지연·시퀀스 시험 때문에 BLOCK이다.
이 결과의 짧은 유효기간은 이미 끝났으며 현재 컷오버 승인에 재사용하지 않는다.

실제 목적지의 동일 계약 수집, 복제 지연 관측, 리허설 시퀀스 결과, 양쪽 통제 시험과 승인된
중앙 증거 보존, T10 실제 계획·권한 결속은 별도 인수 대상이다. 이번 결과는 데이터 행의
동일성·RPO 0·무중단 이전 또는 T19 전체 PASS를 증명하지 않는다.
