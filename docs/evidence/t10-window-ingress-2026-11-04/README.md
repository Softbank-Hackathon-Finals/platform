# T10 실제 창 수신 검증 — 2026-11-04

기존 AWS 플랫폼의 Tailscale Operator에서 **임시 생산자 태그 → 전용 수신 HTTP → 운영 플랫폼 DB** 경로를 확인했다. T10 호스트가 수신하며 T36 #168의 고정 코드 `020c6c78`를 소비한다. T18 담당자의 별도 EC2·실제 앱 부하 인수는 아직 남아 있다. 이 기록은 SHIFT/PASS나 T10 전체 완료를 주장하지 않는다.

| 확인 | 결과 | 근거 |
|---|---|---|
| 로컬 전체 서비스 | 462 passed | [local-test.json](local-test.json) |
| HTTP·저장 집중 시험 | 79 passed, 신규 HTTP 38개 | SQLite와 실제 PostgreSQL; 잠금 대기 중 파일 토큰 교체 포함 |
| 비루트 컨테이너 | 읽기 전용·네트워크 차단 import PASS | [image.json](image.json) |
| migration | 별도 migration 로그인으로 v1 → v2 commit | [migration.json](migration.json) |
| 실제 창 POST | 201, 별도 DB readback과 내용 일치 | [cloud-test.json](cloud-test.json) |
| 거부 | 중복 409, 다른 계획 404, 잘못된 토큰 401, 긴 시각 422, 회수 403 | 같은 실제 수신 주소 |
| 태그 경계 | 허용 태그 정상 HTTP, 다른 태그 TCP 연결 시간 초과 | [transport-controls.json](transport-controls.json), 실제 태그·Ready·nc 실행 확인 |
| DB 권한 | DDL·창 UPDATE·기존 관리 영수증 DELETE 모두 거부 | 운영 runtime 로그인 |
| 재시작 | 수신 Pod 재시작 뒤 같은 창 readback | DB Pod 재시작 아님 |
| 정리 | 임시 자원 10개·이전 이미지 참조 2개 제거, 원문 토큰 삭제 | [cleanup.json](cleanup.json) |

본문 전 인증, 읽는 중 64KiB 제한, 느린 본문 제한, 중복 JSON 키·비유한 수 거부, invalid JSON 400, schema 422, 시각 길이 422도 HTTP 시험에서 확인했다. 인증 뒤 DB 잠금을 기다리는 사이 Secret이 교체되는 반례는 실제 PostgreSQL advisory lock 미획득을 관찰한 뒤 파일만 교체했고, 401과 무저장을 확인했다. DB 권한 확인과 저장은 기존 계획별 트랜잭션에 남아 있다.

## 입력과 실행을 묶는 근거

`image.json`은 허용한 context 파일 해시, upstream commit, OCI index·archive 해시와 runtime config digest를 기록한다. 실제 Pod의 config digest가 archive의 config와 일치하며, 현재 runtime 소스 18개가 이미지 입력과 일치한다. 운영 SQL은 별도 migration SHA-256으로 확인한다.

SSM의 원래 command parameters를 다시 읽어 압축 payload를 풀고 제출 source와 비교했다. [submitted-source.json](submitted-source.json)은 migration·등록·최종 import·검증·정리 5개 작업의 일치를 기록한다. 원본 command/응답, endpoint·UID·생산자 metadata는 비공개로 두고 [receipt-manifest.json](receipt-manifest.json)에 해시만 남긴다. 토큰·DB 암호는 원문 증빙에도 출력하지 않았다.

DB 담당이 12:48 KST에 migration 직전 암호화 백업과 격리 복구를 수행했다. 적용 후 13:06 KST에는 schema 2와 창 2행을 포함한 새 백업·격리 복구가 통과했다. 양쪽 모두 역할·열·제약·객체 ACL·테이블 내용·시퀀스 등 7범주 대조이며, 실제 운영 복구 실행과는 다르다. 이 세션은 기존 DB/Operator/cutover Pod UID와 restart count가 유지됐음을 확인하고 호스트 변경 창을 다음 담당에게 넘겼다.

## 발견한 실패와 교정

1. 첫 이미지의 소스 디렉터리는 비루트 사용자가 읽을 수 없었다. migration·등록 Job이 import 단계에서 실패했고 SQL은 실행되지 않았다. 권한을 수정하고 이미지 실행 검사를 빌드 절차에 넣었다. 그 검사는 upstream 패키지를 얕은 디렉터리에 배치했을 때의 경로 문제도 잡았다. 고정 패키지는 정상 site-packages 위치에 배치했다.
2. 64자 초과 소수초는 T36 검사 뒤 T10 시각 변환에서 ValueError가 나서 503이었다. `_TransactionWindows._us`의 입력 변환 오류만 `invalid_window`로 옮겨 422로 고쳤다. DB 장애를 입력 오류로 바꾸지 않았다.
3. 첫 네트워크 시험의 BusyBox nc는 stdin EOF에서 응답을 읽기 전에 종료했다. DB readback에 창 1행이 있어 결과를 실패 또는 무저장으로 덮지 않았다. 응답까지 stdin을 열어 두도록 시험 도구를 고친 뒤, 새 시각의 별도 창으로 전체 시험을 수행했다. 이전 행도 보존했다. commit 응답 유실을 자동 재전송하는 서버 로직은 없다.

## 보존과 남은 인수

실패·성공 Job 4개, ConfigMap 4개, 시험 ProxyGroup, 생산자 원문 Secret은 UID 조건으로 지웠다. import 임시 archive와 자기 SSM 터널도 정리했다. 이전 이미지 태그 2개를 제거했으며 containerd의 공유 blob 정리는 강제하지 않는다. 로컬 비공개 실패/성공 archive·로그는 재현 증빙으로 보존한다.

수신 Deployment·Service·ServiceAccount·NetworkPolicy, 수신 Operator proxy, 해시 identity Secret은 남긴다. 합성 binding은 revision 2 비활성이고, 계획·창 2행·append-only 영수증도 남긴다. Collector와 Executor의 DB 권한은 늘리지 않았다.

T18의 실제 EC2 위치·실제 계획 ID·토큰 전달 경로는 담당자 확인 대기다. 원문 토큰을 팀원에게 전송하지 않았고 수신 URL도 공개 증빙에 넣지 않았다. 실제 연결은 새 자격과 현재 계획 binding을 발급해 인수한다. T36 담당자는 전용 Operator/태그 경로가 계약과 맞음을 확인했으며, T10 호스트 사용과 오류 코드 시험 결과를 원 스레드에 답했다.

현재 배포는 단일 노드의 검증된 OCI cache를 쓴다. 새 노드·cache 유실 시 재import가 필요하며 지속 registry 배포·HA를 완료한 것으로 보지 않는다. T10 #19의 기존 7개 체크 중 이번 수신부만으로 추가 완료 체크를 만들지 않는다.
