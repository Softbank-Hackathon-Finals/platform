# 팀 실환경 읽기 검증 — 2026-11-04

대상은 실행 중인 출발지 EKS의 demo-chat·PgBouncer·PostgreSQL과 AWS 플랫폼 RKE2의 관측 창 수신기다. **온프렘 목적지 RKE2는 이번 접속 대상이 아니다. T34·T35는 요청에 따라 실행·변경하지 않았다.**

검증 시각은 16:33~16:37 KST다. 플랫폼 코드는 `09f6230` 기준이며, 앱의 실제 실행 이미지 digest는 결과 JSON에 따로 기록했다. 로컬 demo-chat checkout이 배포된 버전과 같다고 가정하지 않았다.

운영자 시험 스크립트를 기존 Pod의 Python으로 실행했다. 앱의 AWS 호출에는 그 Pod의 IRSA 역할을 사용했고, 관리자 SSO를 앱 역할의 대조군으로 사용하지 않았다. DB 비밀번호는 앱 프로세스 메모리에서만 사용했다. 업무 데이터·권한·쓰기 차단·트래픽·클러스터 설정은 변경하지 않았다.

## 실제 확인 결과

| 작업 | 시험 | 관측 결과 | 범위 |
|---|---|---|---|
| T14 | 앱·DB health / readiness | HTTP 200 | 실제 출발지 |
| T14 | 앱 역할의 자기 DB Secret 읽기 | HTTP 200 | 다른 앱 Secret 거부는 미시험 |
| T14 | 정상 CA·호스트로 DB 연결 | TLS 사용, `SELECT 1` 성공 | 잘못된 호스트 시험 전후 모두 성공 |
| T14 | 잘못된 서버 이름 | 인증서 이름 불일치로 거부 | 네트워크 timeout을 거부로 세지 않음 |
| T14 | 신뢰되지 않은 CA | 인증서 검증 실패(code 20) | PostgreSQL SSLRequest 후 TLS handshake, 인증정보 전송 없음 |
| T14 | 평문 PostgreSQL startup | ErrorResponse, SQLSTATE `08P01`, `SSL required` 또는 `TLS required` | 비밀번호를 전송하지 않음. 평문 SQL 실행 시험은 아님 |
| T14 | DB 앱 역할 | superuser / create role / create DB / replication / bypass RLS 모두 false | 역할 속성 확인; 타 앱 DB 접근 시험은 아님 |
| T14 | 기존 첨부파일 읽기 대조 | 앱 GET 200 → 무서명 GET 403 AccessDenied → 앱 GET 200 | 양쪽 200 응답의 VersionId가 사전 조회와 일치 |
| T14 | 관리자 API 인증 | 토큰 없음·잘못된 토큰 모두 401 | 조회 경로만 사용; 쓰기 차단·전체 WebSocket 종료 실행 안 함 |
| T22 | 메시지 조회 | HTTP 200, 응답 목록 확인 | 내용·작성자 미기록, 새 메시지 생성 없음 |
| T22 | WebSocket | 연결 및 `hello` 수신, 시험 연결만 1000으로 종료 | 1012 강제 종료·목적지 재연결은 미시험 |
| T36 | 운영 창 수신기의 health / DB readiness | HTTP 200, `no-store` | 기존 T10 배포본 |
| T36 | 미인증 창 POST | HTTP 401, `no-store` | 창·계획·생산자 등록 없음 |
| T33 | 실제 Security Hub GetFindings 오류 | 기존 수집기가 FAILED, 화면 요약이 UNKNOWN, 커서 미전진 | AWS 서비스 미활성 상태. 정상 0건으로 처리하지 않음 |

S3 시험은 일반 `GetObject`이며 `VersionId` 요청 파라미터를 사용하지 않았다. `GetObjectVersion` 권한과 혼동하지 않기 위해 반환된 현재 버전만 대조했다. 객체 본문은 읽거나 저장하지 않고 응답 스트림을 닫았다. 정책·객체를 원자적으로 고정한 스냅샷 검증은 아니다.

## 완료로 처리하지 않은 항목

| 작업 | 남은 인수 조건 |
|---|---|
| T14 | 목적지 등록·접속, 양쪽의 필수 시험 집합, 신뢰된 수집기 등록과 T5 ControlRun 생성. 이 보고서의 운영자 점검을 제품 시험기의 PASS로 수입하지 않는다 |
| T19 | 실제 양쪽 ControlRun·계획·서버 승인 결속과 데이터 준비 판정의 결합. 목적지가 없어 모의 결과로 대체하지 않았다 |
| T21 | 실제 사용자 로그인 → 콘솔·SSE → 승인 → 실행의 통합 검증. 접속한 플랫폼에는 관측 창 수신기만 확인됐으며 전체 콘솔을 검증하지 않았다 |
| T22 | 실제 양쪽 환경 쓰기·첨부파일 보존·1012 재연결·컷오버. 이번 읽기 성공은 무중단 이전 성공이 아니다 |
| T33 | 실제 탐지 공급자 활성화, 실제 이벤트 수집, 호스팅 API 연결. Security Hub 미가입·GuardDuty detector 0개는 안전하다는 증거가 아니다 |
| T36 | 실제 T18 생산자 자격·등록된 계획으로 창 전송·DB 저장·판정 연결. 현재 부하에서 오류율·p95 임계값은 측정하지 않았다 |
| T37 | Prometheus·Collector·조회 API 실제 배포, source/target 경로 및 인증서, 앱 지표 송신. 출발지 `/metrics`는 404였음 |

T37의 기존 용량 설명은 과거 측정이다. 이번 플랫폼 노드의 allocatable CPU는 **4 vCPU**였다. 따라서 2 vCPU 부족을 현재 배포 차단 근거로 재사용하지 않는다. 실제 지표 경로에는 T30의 tailnet ACL·egress 및 T6/T30의 DB 접근 조건과 해당 담당의 범위 합의가 필요하다. 이번 점검은 그 설정을 변경하지 않았다.

T30 목적지 연결 정보 인계는 [기존 요청](https://github.com/Softbank-Hackathon-Finals/platform/issues/17#issuecomment-6623118355)과 구분 없이 중복 등록하지 않는다. 플랫폼 RKE2의 인증 성공을 목적지 인수로 대체하지 않는다.

## 증거와 재현 경계

- [출발지 보안 관측](source-security.json)
- [실제 AWS 수집 실패 처리](findings-unavailable.json)
- [관측 수신기·지표 준비 상태](observability-readiness.json)
- [실제 앱 읽기·WebSocket](app-readiness.json)

원본 결과·실행 스크립트·Pod/ServiceAccount/Deployment 메타데이터는 운영자 비공개 검증 디렉터리에 보존했다. 공개 JSON에는 원본 SHA-256과 가린 결과만 담았다. 해시는 서명이나 제3자 검증을 의미하지 않는다. AWS 수집 실패 처리는 기존 `bravo_findings.collectors.run_once`와 `View`로 실행했으며 SDK 모의 응답을 사용하지 않았다. 그 밖의 점검은 운영자 스크립트로 실행했으므로 정식 T14 전체 수집 체인의 자격을 부여하지 않는다.

처음 앱 Pod의 기본 Python에는 boto3가 없어 실행이 중단됐다. 실제 앱의 `/app/.venv/bin/python`을 확인해 다시 실행한 결과만 표에 반영했다. 원본 실패 기록도 보존했다. 목적지·생산자 권한·승인·감사 수집·백업을 대신 만들거나 완료 표시하지 않았다.
