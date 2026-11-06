# T14 하위 시험별 구현·인수 상태

2026-11-06 14:15 KST 확인 기준. 완료 여부의 정본은 [T14 이슈](https://github.com/Softbank-Hackathon-Finals/platform/issues/21)다.
이 표는 **시험 항목이 존재함**, **검사 코드가 있음**, **운영 호스트가 실제 실행함**을 구분한다.
설계/프로필의 20행을 생성하는 것만으로 20개를 검사했다고 판단하지 않는다.
PR에만 있는 구현은 링크로 표시한다. 해당 PR의 최신 병합 상태와 실제 운영 배포는 별도로 확인한다.

실제 출발지 결과는 서로 다른 실행일 수 있으며 만료 시 다시 수집해야 한다. 아래 결과를
합쳐 새로운 full PASS로 만들지 않는다. 실제 온프렘의 개인 접속·시험 주체·등록 원문은 별도
인수 대상이며 AWS의 플랫폼 RKE2를 목적지로 대체하지 않는다.

| 시험 | 검사 코드 | 실제 출발지와 남은 연결 |
|---|---|---|
| SEC-01.1 다른 역할의 Secret 읽기 | `aws_probes.py`, `aws_qualification.py`, `openbao.py` | 두 실제 시험 주체·동일 Secret 인수 필요. 앱 Secret의 메타데이터 확인은 이 시험을 대신하지 않음 |
| SEC-01.2 소스·설정 비밀 탐지 | `secret_scan.py` | [#356](https://github.com/Softbank-Hackathon-Finals/platform/pull/356): 등록 revision 전체 소스·실행 설정 검사. 목적지 산출물은 별도 |
| SEC-01.3 로그 비밀 노출 | `secret_logs.py`와 [#427 앱 카나리 실행기](https://github.com/Softbank-Hackathon-Finals/platform/pull/427) | [고정 로컬 demo-chat/PG의 주입·복원](demo-log-canary.md), 정상·누출·로그 누락 검증. 운영 효과 실행부·CloudWatch 전달 완료·배포 이미지·양쪽 로그 원천 인수 필요 |
| SEC-01.4 비밀 회전 후 회복 | `secret_rotation.py` 읽기 전용 판정·격리된 실제 앱/PG 시험 | [회전 시험](secret-rotation.md): 재시작·기존 연결·30초 초과 구분. 운영 Secret/CNPG/풀러 적용·실행권·복구 및 양쪽 접점은 별도 필요 |
| ENC-02.1 Secret 저장 암호화 | `secret_encryption.py`, `target_secret_encryption.py` | #341 실제 AWS 메타데이터. [#425](https://github.com/Softbank-Hackathon-Finals/platform/pull/425) 실제 OpenBao 격리 시험과 AWS **플랫폼** RKE2 암호화 CLI 조회. 온프렘 목적지·전체 노드·저장 전달 결속 인수는 별도 |
| ENC-01.1 앱→Pooler TLS | `postgres_tls.py`, `app_process_tls.py` | [#298](https://github.com/Softbank-Hackathon-Finals/platform/pull/298): 출발지 실제 앱 경로. 목적지 실행 필요 |
| ENC-01.2 잘못된 CA/호스트 거부 | 같은 TLS 수집기 | #298 출발지 실제 거부·대조군. 목적지 실행 필요 |
| ENC-01.3 평문 거부 | 같은 TLS 수집기 | #298 출발지 실제 거부·대조군. 목적지 실행 필요 |
| ENC-01.4 복제 TLS | `replication_tls.py` | 고정 SQL 조회·슬롯/sender 결속 구현. T8의 실제 설정 적용 영수증·양쪽 읽기 접점 인수 필요 |
| ENC-01.5 Pooler→DB TLS | `pooler_backend_tls.py` 고정 SQL 수집기 | [실제 PG/PgBouncer 격리 시험](pooler-backend-reader.md): verify-full/verify-ca/평문/재사용/잘못된 CA 구분. 운영 앱·stats-read 주체와 등록·저장 인수 필요 |
| AUD-01.1 관리자 접속 검색 | `audit_probes.py`, T34 수집기 | 실제 CNPG 감사 원천→저장소 전달·최소 권한 검색 인수 필요 |
| AUD-01.2 DDL 검색 | 같은 감사 수집기 | 승인된 앱 DB 시험 스키마와 실제 생성/삭제·중앙 검색 인수 필요 |
| AUD-01.3 권한 변경 검색 | `audit_probes.py`와 CloudWatch 출처 검증 | [#430](https://github.com/Softbank-Hackathon-Finals/platform/pull/430): 실제 EKS 시험 namespace의 새 RBAC 이벤트를 약 16초에 수집. 앱 namespace·목적지·최소 권한 조회 주체·새 계획 연결은 별도 |
| NET-01.1 외부 공개 포트 | [#388](https://github.com/Softbank-Hackathon-Finals/platform/pull/388)의 `public_ports.py` Nmap 스캔·판정 | 실제 격리 loopback Nmap 검증. 운영 양쪽 등록 주소/포트 범위·ingress/node inventory·실제 외부 실행 주체·허용 기준선 및 대조군 인수 필요 |
| NET-01.2 외부→DB 차단 | `network_probes.py`, [#436 역할별 리스너 후보](https://github.com/Softbank-Hackathon-Finals/platform/pull/436) | 모의·로컬 검증. 실제 외부/허용 주체·DB/풀러 등록·정책 readback 인수 필요. 고정 5432/6432 방법 1과 등록 리스너 방법 2를 섞지 않음 |
| NET-01.3 다른 namespace→DB 차단 | 같은 네트워크 수집기 | [#432](https://github.com/Softbank-Hackathon-Finals/platform/pull/432) 방법 1은 풀러 대조군 포트 차이로 UNKNOWN. [#436](https://github.com/Softbank-Hackathon-Finals/platform/pull/436) 별도 방법 2의 실제 등록 DB·풀러 TCP는 모두 연결되어 **FAIL**. SQL/로그인/데이터 접근 시험 아님. 방법 2 프로필·현재 계획·목적지 인수 필요 |
| IAM-01.1 익명 객체 읽기 | AWS 객체 수집기 | #298/#341 실제 출발지. 목적지 SeaweedFS 주소·자격·객체 버전 연결 별도 |
| IAM-01.2 기준선 초과 권한 | `tool_reports.py`, [#418 `prowler_iam.py`](https://github.com/Softbank-Hackathon-Finals/platform/pull/418) 정책 원문·도구 진단 검증 | [실제 Prowler 5.47.0 자격 시험](../../../docs/evidence/t14-prowler-2026-11-06/README.md): 역할 필터 누락/분석 오류와 내부 오류 후 PASS 반환을 UNKNOWN으로 차단. 실제 점검 ID 두 개·정책 지문 대조 구현. 전체 Prowler/kube-bench 매핑·수동 점검·양쪽 기준선은 미확정이며 manual/WARN도 PASS로 바꾸지 않음 |
| ISO-01.1 다른 앱의 객체·DB 접근 | [#395 `app_isolation.py`·`postgres_isolation.py`](https://github.com/Softbank-Hackathon-Finals/platform/pull/395) | 결합 시험기와 실제 로컬 PG 18.4 거부/허용/잘못된 비밀번호/감사 누락 검증. 객체 수집기는 SDK Stubber 범위. 실제 두 앱의 AWS/SeaweedFS·DB 자격과 양쪽 감사 원천 인수 필요. NAT/Pooler 경유는 미인수 |
| DR-01.1 새 클러스터 복원 | `backup_restore.py`, `cnpg_*`, `postgres_restore.py` | 로컬 자격 경로 있음. T35 양쪽 실제 저장소·archive 준비·표식 경계·신규 복원·검증 인수 필요 |

## 실제 완료를 위해 남은 작업

1. 코드 자격과 운영 연결을 구분해 인수한다. 회전·외부 포트·앱 간 격리·로그 카나리의
   로컬 실행 경로는 준비됐으나 운영 적용·복구 창과 양쪽 등록이 필요하다. Prowler의 실제
   분석 오류 해결, kube-bench의 점검 ID·역할·namespace 매핑은 코드/기준 작업도 남아 있다.
2. 존재하는 검사기에 승인된 실행 주체·원천·결과 저장소를 연결한다. 운영 설정 변경이 필요한
   시험은 기존 담당의 변경 창과 맞추고 실패/응답 유실 시 복구·정리 근거도 보존한다.
3. T10이 프로필을 고정한 새 운영 계획에 각 실제 실행 원문을 등록한다. T19는 동일 계획·앱·환경
   쌍과 현재 유효한 증거만 비교한다. 목적지 누락은 차단이며 임의 fixture로 채우지 않는다.
4. T21에 부분 완료·UNKNOWN 원인·운영 증거 범위를 표시하고 T22 실제 앱 시연과 연결한다.

T34·T35는 T14의 감사/복구 시험 실행을 공급하고, T36·T37은 관측 판정과 수집 경로를 공급한다.
이 태스크들의 코드 병합을 T14의 실제 양쪽 통과로 계산하지 않는다.

## T19로 넘기는 원문

[#440](https://github.com/Softbank-Hackathon-Finals/platform/pull/440)은 네트워크 집계와 DB·풀러 자식 원문을
모두 인증하는 T19 연결이다. 합성 입력의 코드 연결 시험이며 운영 양쪽 PASS가 아니다.
하나의 원문·정책 hash·전후 대조군·현재 run 결속이라도 확인되지 않으면 전체 NET 통과로 쓰지 않는다.
방법 2 파싱 지원은 기본 T5 카탈로그나 기존 계획의 자동 갱신을 뜻하지 않는다.

## 운영 인수에 필요한 접점

| 접점 | 필요한 인계·작업 | 현재 범위 |
| --- | --- | --- |
| T6·T30 / 실제 환경 | 양쪽 앱·객체·DB·Secret·ServiceAccount 등록과 시험 주체, 대상 환경 개인/서버 접속 경로 | 플랫폼 RKE2를 온프렘으로 대체하지 않음. 자격 값은 공개 기록에 남기지 않음 |
| T6·T8·T35 / DB 변경 창 | 회전·감사 카나리·백업 플러그인/아카이브 적용과 복구 순서 | 공유 DB 재시작·자격 회전을 독립 시험이라는 이유로 실행하지 않음 |
| T34 / 감사 검색 | 양쪽 원천→중앙 저장, 세 카나리의 완료 검색, 최소 권한 조회·권한 없는 주체 거부 | EKS RBAC 한 종류의 실제 성공이 DB 접속·DDL·목적지 전체 성공을 대신하지 않음 |
| T10·T19 / 판정 입력 | 프로필이 고정된 현재 계획, 인증된 원문 저장/조회와 현재 실행권 | 과거 원문의 run ID·시각을 바꿔 새 계획 입력으로 만들지 않음 |

출발지 NET 시험의 FAIL은 검증을 수행한 결과다. 이를 숨기거나 UNKNOWN과 합쳐 "미실행"으로
표시하지 않는다. 출발지 FAIL·목적지 PASS를 개선으로 보는 기존 T5 규칙도 유지한다.

## 출발지 정책 집행 기능의 별도 확인

실제 EKS의 VPC CNI 정책 집행은 켜져 있었다. 별도 소유 namespace에서 Deployment가 만든
세 Pod로 정책 적용 전 연결, 적용 후 허용 주체 연결·다른 주체 타임아웃을 확인했다.
[시험 기록](../../../docs/evidence/t14-network-enforcement-2026-11-06/README.md)에 범위와 한계를 남겼다.
이는 출발지 앱/DB 정책 수정이나 NET-01.3 PASS가 아니다. 운영 namespace의 정책 선택 범위와
DB·풀러 경로를 따로 인수해야 한다. 앞선 실제 DB 시험은 단독 Pod를 사용했으므로,
거부 성공을 자격화하는 후속 시험은 CNI가 지원하는 컨트롤러 소유 Pod로 수행한다.
