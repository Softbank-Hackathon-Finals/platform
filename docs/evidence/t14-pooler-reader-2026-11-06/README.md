# T14 ENC-01.5 고정 SQL 수집기 자격 시험

2026-11-06 11:17:30–11:17:39 KST, 소유한 로컬 Docker 격리 환경에서 실행했다.
PostgreSQL 18.4와 공식 PgBouncer 1.25.1을 사용했다. 팀 AWS/온프렘 운영 결과가 아니다.

| 실제 조건 | 수집기 관측 |
|---|---|
| verify-full, 새 backend, TLS 1.3 | enabled |
| 같은 Pooler의 기존 backend 재사용 | UNKNOWN / UNEXPECTED_RESPONSE |
| verify-ca, TLS 1.3 | disabled: hostname 검증 기준 미충족 |
| backend TLS disable, ssl=false | disabled |
| Pooler backend에 신뢰하지 않는 CA 지정 | UNKNOWN / TOOL_ERROR: 연결 실패를 안전한 TLS로 판정하지 않음 |

모든 경우 frontend는 인증서를 검증하는 TLS를 사용했다. 동일 수집기가 `SHOW CONFIG` 전후
조회와 실제 앱 역할의 READ ONLY 트랜잭션에서 backend 정보를 수집했다. 고정 listener·계정·
등록 전후 결속, 전용 연결 정리, 원문 오류 비노출은 별도 회귀 시험으로 확인했다.

- PostgreSQL 시험 이미지: `sha256:53fac2d9ca481e9029043060f1d2d183234451f2990de9bafd41c51b8eccc51d`
- PgBouncer: `ghcr.io/cloudnative-pg/pgbouncer@sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8`
- network none과 공유 격리 loopback, 비 root, 포트 미공개, capability 제거, 자원 제한.
- 일회용 자격·인증서로 시험. CA 개인 키는 컨테이너에 전달하지 않았다.
- 성공한 마지막 실행에서 생성한 컨테이너·임시 파일 정리 완료.

첫 실행은 이미 삭제한 컨테이너를 재확인할 때 Docker의 소문자 `no such object`를
인식하지 못해 정리 오류가 났다. 소유 label을 확인해 남은 시험 DB를 정리하고,
한 자원 정리 실패가 나머지 정리를 건너뛰지 않도록 고친 뒤 전체를 다시 실행했다.

이번 시험은 데이터 유실·운영 가용성·복제 TLS를 증명하지 않는다. 팀 운영 환경의 앱/통계
읽기 주체와 등록·영속 저장, 새 backend 확보 및 시계 동기화 인수는 남아 있다.
미등록 터널을 신뢰하지 않으며 운영 연결을 끊거나 설정을 바꾸어 PASS를 만들지 않는다.

[재현 및 운영 호스트 계약](../../../services/control-runner/docs/pooler-backend-reader.md)
