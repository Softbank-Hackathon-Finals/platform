# T14 SEC-01.4 로컬 앱 회전 시험

날짜: 2026-11-06. 범위: 격리된 실제 PostgreSQL 18.4와 demo-chat HTTP/DB 코드.
운영 AWS/OpenBao 제공자·팀 공유 DB·PgBouncer는 이 시험 대상이 아니다.

| 항목 | 결과 |
|---|---|
| 실제 앱의 자격 갱신·재접속·신규 단일 행 저장 | 276ms, 재시작 없음 |
| 기존 자격으로 새 인증 | 거부, 새 자격의 별도 연결은 성공 |
| 시험 전 앱 DB 세션 | 1개 종료, 새 커밋 backend 시작 시각 확인 |
| 실제 앱 프로세스 재시작 후 동일 판정기 재조회 | 실패로 구분 |
| 자격 공급원을 기존 값으로 고정한 대조군 | 30,447ms 관측 후 FAIL, 새 행 0개 |
| DB 전송 | verify-full / SCRAM / channel binding |
| 생성한 컨테이너와 임시 파일 | 정리 완료 |

사용한 demo-chat revision: `f42e25d04fd4fe82b9d5bb6d9fa63eda1511936f`.
qualification image: `sha256:53fac2d9ca481e9029043060f1d2d183234451f2990de9bafd41c51b8eccc51d`.
의존성은 [시험 이미지 목록](../../../services/control-runner/testing/rotation-requirements.txt)에 고정했다.
시험은 이미지 ID를 고정한 뒤 실행하며 외부 네트워크·공개 포트·root 실행을 사용하지 않는다.

앱의 Secrets Manager 읽기 함수는 로컬 비밀 파일 읽기로 대체했다. 나머지 앱의
DB pool·인증 실패 후 캐시 무효화·HTTP·SQL 쓰기 코드를 실행했다. DB 시험 trigger로
각 쓰기 backend를 기록했다. 이 결과는 SDK 제공자 회전, ESO 전파, 운영 풀러 갱신이나
운영 사용자 요청의 가용성을 증명하지 않는다.

회귀 시험은 현재 자원/버전 결속, 재시작, 기존 연결, 중복 표식, 30초 경계, 누락·만료·
시계 불일치, 원문 저장 실패를 검사한다. full 프로필에서는 이 시험 외 19개를 UNKNOWN으로
유지하며, 시험기 예외 문자열에 있는 비밀이 결과에 포함되지 않는지도 확인한다.

재현 절차와 실제 운영 인수 조건은 [비밀 회전 검사](../../../services/control-runner/docs/secret-rotation.md)를 따른다.
이 자료로 T14 양쪽 완료 체크나 T19 full PASS를 만들지 않는다.
