# T8 재개 도구 결함 수정과 앱 TLS 재시험

2026-11-05, 류지환. #207의 원 구현과 과거 측정은 그대로 보존하고, 후속 커밋에서 두 결함을 수정했다. [기계 판독 요약](summary.json)은 수정 소스의 커밋·SHA-256과 비공개 실행 자료의 SHA-256을 포함한다. 이 기록은 현재 클라우드↔로컬 복제 복구 성공을 뜻하지 않는다.

## 두 결함

| 조건 | #207 `9612f94` 재현 | 수정 후 실제 PostgreSQL 18.4 결과 |
| --- | --- | --- |
| 잘못된 DB에서 `public.current_database()`와 `search_path`로 신원 위조 | 제한된 probe 역할·`verify-full`에서도 다른 DB에 시험 행 1개 삽입 | catalog 신원 확인으로 거부, 양쪽 0개. 올바른 DB의 같은 역할은 삽입·삭제 성공 |
| 서버가 COMMIT 중 대기할 때 클라이언트 응답 유실 | 양쪽 행이 아직 없다는 이유로 정리 완료·커밋 0 보고. 나중에 양쪽 1개 잔류 | 동일 UUID의 transaction lock 때문에 정리 보류·커밋 미확인. COMMIT 종료 후 삭제와 목적지 적용 위치를 확인하고 양쪽 0개 |

읽기 연결의 `search_path`를 고정하고 핵심 신원 함수를 `pg_catalog`로 한정했다. INSERT는 잠금을 얻었다는 응답 뒤에만 보내고 DELETE도 같은 잠금을 얻는다. 목적지 INSERT를 관측하지 못했다면 목적지 행 부재만으로 완료하지 않고 DELETE 커밋 응답 뒤 읽은 WAL 위치 이상이 적용됐는지도 확인한다. 이전 도구의 기록은 이 잠금 보장이 없어 `--marker-protocol` 없는 정리 명령을 거부한다.

회귀 시험은 실제 TLS 연결, 두 DB의 logical replication, 늦은 COMMIT과 응답 유실을 사용한다. DB는 digest로 고정한 일회용 Docker 컨테이너이며 공유 클라우드에 접속하지 않는다. 143개 재개 도구 단위 시험과 두 wire 시험이 통과했다. 전체 저장소 시험은 Python 3.12에서 740개 중 733개 통과·7개 건너뛰기였다. 실제 재개 CLI의 12개 실패·재실행 경로도 모두 통과했고, 연결을 끊고 약 80 MiB 이상의 행 WAL을 쌓은 뒤 복구하는 로컬 시험 1회에서 ACK 받은 행의 유실 0개·업무 표 일치·시험 행 잔류 0개를 확인했다. 컨테이너·네트워크는 소유 라벨을 대조하고 제거했으며, 남은 시험 행과 소유 리소스는 0개였다.

```sh
python3 -m unittest discover -s scripts/tests -p test_resume_replication.py -q
T8_RESUME_WIRE=1 uv run --with pytest --with 'psycopg[binary]==3.3.6' \
  pytest infra/envs/target-workloads/testing/test_resume_replication_wire.py -q
```

## 이상무님과 같은 앱 TLS 시험

현재 기본 브랜치의 앱 프로세스 collector/client는 이상무님이 시험한 `278d2a8`의 파일과 동일함을 바이트로 대조했다. 11-05 **17:54:51–17:55:38 KST**에 기존 AWS 앱 프로세스에서 실행했다. 시작 전·각 probe 전후·종료 후 총 8회 Kubernetes 바인딩을 읽었으며 앱/Pooler/배포/설정/이미지/재시작 상태가 같았다. 실행 자료를 호스트에 저장하고 fsync한 뒤 동일 run ID와 digest로 ACK했다.

| 시험 | 실제 AWS 앱 | 로컬 PG18.4 + PgBouncer 1.25.1 |
| --- | --- | --- |
| ENC-01.1 정상 `verify-full` 및 고정 `SELECT 1` | PASS | PASS |
| ENC-01.2 잘못된 CA·호스트명 거부, 전후 정상 대조 | PASS | PASS |
| ENC-01.3 비밀번호 없는 평문 시작 거부, 전후 정상 대조 | PASS | PASS |

로컬에서는 ACK 유실·이전 ACK를 UNKNOWN/TOOL_ERROR, 잘린 오류·잘못된 인증 응답을 UNKNOWN/UNEXPECTED_RESPONSE로 판정했다. 새 ACK 재시도는 PASS였다. 로컬 바인딩 조회는 시험용 등록 자료이고 실제 Kubernetes 증거는 AWS 실행에만 해당한다. 고정 PgBouncer 컨테이너 이미지를 가져오는 첫 시도는 registry `denied`로 중단됐고, 로컬 대체 시험은 같은 버전의 native binary를 사용했다.

범위는 **앱→Pooler TLS**다. 복제 TLS(ENC-01.4), Pooler→DB TLS(ENC-01.5)와 나머지 15개 항목은 실행하지 않아 UNKNOWN으로 남겼다. negative probe에 비밀번호를 보내지 않았고, DB 쓰기·설정 변경은 없었다. 임시 실행 파일과 이 실행이 연 개인 SSO 터널은 종료 후 제거했다. 자격 증명·실제 엔드포인트·Pod UID는 공개하지 않는다.

## 남은 클라우드↔로컬 복구

11-05 16:30 조회에서는 정방향 구독 OFF, worker 0, 기존 슬롯 유효, 양쪽 메시지·첨부 메타데이터 일치였다. 같은 TLS 경로의 신원 조회 성공은 복제 재개 성공을 뜻하지 않는다. 11-04의 성공 기록은 [#201](https://github.com/Softbank-Hackathon-Finals/platform/pull/201)에 있다.

이번 실행은 ENABLE과 공유 DB 시험 행 삽입을 아직 하지 않았다. T8 담당과 범위·실행 소유자를 맞추고 T12 작업이 끝난 시간을 확인한 뒤, 최신 DB 신원·기존 슬롯·실행/lease/fence·단일 쓰기 상태를 다시 읽어야 한다. 이후 기존 구독 1회 재개, 고정 LSN 통과, 소유 UUID의 삽입·삭제 전달, 양쪽 잔류 0개와 업무 데이터 일치를 확인한다. 역방향 OFF·목적지 앱 NOLOGIN·출발지 쓰기 차단을 유지한다.

이번 코드는 연결 단절을 막거나 timeout/`disable_on_error` 정책을 바꾸지 않는다. 5초 타임아웃 뒤 정방향 OFF가 되는 운영 조건과 설정 변경은 별도 판단이 필요하다. T8 전체 완료나 컷오버 성공으로 집계하지 않는다.
