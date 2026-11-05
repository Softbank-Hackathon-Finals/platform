# T10 게이트 원문 저장 — 실제 T7 DB 연결 검증

2026-11-05 18:36 KST, 등록된 AWS T7 RKE2에서 기존 플랫폼 DB의 runtime 역할로
실행한 검증 Job이 **12/12 통과**했다. 실제 출발지 T14 원문을 저장하고 새 연결로
읽어 같은 해시·판정을 확인했다. 목적지 원문이 없으면 게이트 결속을 거부했다.
이 결과는 T10 저장·권한 경계의 E2E이며 T19 전체 운영 인수나 컷오버 완료가 아니다.

## 합의와 실행 범위

- [상무님 17:57 범위 동의](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793869020870859):
  목적지 실제 결과·원문은 아직 전달 가능한 상태가 아니므로 누락 차단부터 검증한다.
- [T19 운영 인수 #29](https://github.com/Softbank-Hackathon-Finals/platform/issues/29):
  담당은 이상무, In progress 1/2다. 이 PR은 T10 저장·조회 접점을 추가하며
  T19 판정·생산자 인증 규칙을 변경하지 않는다.
- T10 호스트 작성자와 새 `gate_records.py`만 맡기로 분담했다. 기존 인증 호스트·
  리허설·ProductInput 작업과 분리했다. T12 공유 실행 창 반환을 확인하고 실행했으며,
  18:43 정리 영수증을 전달했다. 시간 경과를 실행 허가로 사용하지 않았다.
- T7 DB·Secret·runtime 역할을 그대로 사용했다. 임시 Job·ConfigMap·전용
  NetworkPolicy만 만들었다. 업무 DB·등록 계획·기존 Deployment·Executor는 변경하지 않았다.

## 실제 입력과 측정

출발지 생산자가 17:54:51–17:55:38에 측정해 인계한 원문을 수정 없이 사용했다.
원문 SHA-256과 T19 정규화 해시는 모두
`3b3a8f99c3dd1e2b50356754c52fecc9a2a9a644a8e8a71cdcfb9e925db2d762`다.
ENC-01.1·01.2·01.3은 PASS, 나머지 17개는 UNKNOWN/NOT_RUN이다.
이번 Job이 통제 시험을 다시 실행한 것은 아니다.

| 검증 경계 | 실측 결과 |
|---|---|
| DB 연결 | TLS 인증서·호스트 검증, 제한된 runtime 역할 확인 |
| DB 버전·상태 스키마 | PostgreSQL 18.4, `state_schema_versions=[2]` |
| 원문 저장·중복 요청 | 같은 참조·같은 원문 멱등 저장 |
| 연결을 닫고 재조회 | 원문 hash 일치, PASS 3 / FAIL 0 / UNKNOWN 17 유지 |
| 다른 tenant·권한 철회 | 모두 조회 거부 |
| 같은 참조의 내용 교체 | 거부 |
| 실제 목적지 결과 누락 | 결속 거부, 컨텍스트·판정 생성 0 |
| 상태·외부 효과 | 계획 COLLECT 유지, Executor 호출 0 |
| Job | 18:36:00–18:36:16 KST, 종료 코드 0, 컨테이너 재시작 0 |
| 정리 | UID 전제조건으로 자원 3개 삭제, Pod 부재, 기존 Pod UID·컨테이너별 재시작 불변 |

검증용 tenant와 계획은 운영 환경 등록이 아니다. 계획·원문·권한 회수 이력은
기존 append-only 정책대로 남겼다. 정리를 위해 DB 삭제 권한을 추가하지 않았다.
실제 운영 tenant와 등록 계획 목록도 이번 범위에서 조회하지 않았다.

첫 실행은 묶음에서 PostgreSQL 드라이버가 빠져 **DB 접근 전 import 단계에서 실패**했다.
임시 자원과 Pod를 정리한 후, `platform-events`의 `postgres` extra를 잠금 파일의
해시와 함께 내보내도록 수정했다. 재실행에는 DB 접근 전 필수 모듈 import 검사도 넣었다.
실패 기록은 성공 기록과 별도로 보존했다.

## 구현과 로컬 검증

기존 계획별 트랜잭션과 `plan_commands`를 사용한다. 테이블·DB·관리 API를 추가하지
않는다. 읽기·원문 등록·결속·판정 권한을 구별하고 매 요청의 현재 권한을 확인한다.
컨텍스트는 revision을 대조해 결속하며, 과거 요청의 재시도는 이전 결속을 재활성화하지 않는다.
T19 `CombinedGateService`와 현재 신뢰·준비 판정 공급자는 그대로 사용한다.

| 로컬 검사 | 결과·한계 |
|---|---|
| platform-events 전체 | 912 passed, 기존 경고 1개; 새 저장 경계 32개 포함 |
| T19/T10 소비 계약 | 51 passed |
| 콘솔 | 82 passed |
| 최종 원문 비교 수정 후 저장 경계 | SQLite·실제 로컬 PostgreSQL 32 passed |
| 정적 검사 | Ruff·diff·공개 증빙·소스 manifest 검사 통과 |

로컬 생산자·인증·목적지 결과는 합성이다. 실제 클러스터에서는 위 표의 저장·조회·
누락 차단 범위만 검증했다. 런타임 소스는 `97b1a916156772edcafa8c26a780568b8d135ab4`,
기반 develop은 `4c196627375ff9c6f6890e9a55456c56c5f2bcef`다.

## 재현과 증빙

[호스트 접점·probe 사용법](../../../services/platform-events/testing/GATE-RECORDS.md)을 따른다.
실행 창을 조율하고 고유 `BRAVO_PROBE_ID`를 사용한다. 기존
`infra/envs/platform-data/probe_job.py`의 보안 설정·마운트·해시 검증을 재사용하며,
원문을 포함한 실행 묶음과 운영 매니페스트는 비공개로 보존한다.

외부 의존성은 `services/platform-events`에서 다음과 같이 내보낸다.

```sh
uv export --locked --no-dev --extra postgres \
  --no-emit-project --no-emit-package bravo-observability -o requirements.txt
```

T10·T19·observability 소스와 계약 스키마, probe를 같은 묶음에 넣는다.
원문을 인수한 경우에만 `BRAVO_PROBE_SOURCE_PATH`를 전달한다. `--require-hashes`
설치와 import 검사를 통과한 뒤 probe를 실행하고, 생성 UID로만 정리한다.
실제 원문·운영 접속 정보는 공개 증빙에 복사하지 않는다.

- [runtime.json](runtime.json): 운영 식별자를 제외한 검토된 요약. 원본 그대로가 아니며,
  비공개 원본 hash와 측정 결과·시간·정리 결과를 기록한다.
- [source-manifest.json](source-manifest.json): 측정 커밋의 주요 소스 파일 hash.
  실행 묶음 hash는 runtime.json에 별도로 기록했다.

## T19 운영 인수에 남은 조건

| 조건 | 다음 책임·상태 |
|---|---|
| 양쪽 필수 통제의 실제 T14 원문과 생산자 신뢰 | 현재 source 3 PASS / 17 UNKNOWN, target 없음. T14/T19 담당 인수 대기. source를 target으로 복사하지 않음 |
| 실제 같은 앱·출발지·목적지·계획의 등록과 현재 권한 | T10 운영 인증·등록 담당과 연결. 이번 검증용 계획은 사용하지 않음 |
| 원문 쌍의 열린 버킷·스키마 불일치 BLOCK, 교정 뒤 PASS | 상무님 T19 남은 인수 항목; 실제 양쪽 입력으로 확인 필요 |
| T13 준비 판정·현재 보류/승인·SHIFT·실제 Executor | 각 운영 공급자와 결속 후 별도 통합 시험 필요 |

T7·T10 원래 완료 체크는 유지하고 T19 1/2를 추가 체크하지 않는다. 저장 경로가
통과했다는 이유로 아직 없는 목적지 결과, 현재 실행 허가, 전체 해피패스를 주장하지 않는다.
