# T14 하위 시험별 구현·인수 상태

2026-11-06 작업 기준. 완료 여부의 정본은 [T14 이슈](https://github.com/Softbank-Hackathon-Finals/platform/issues/21)다.
이 표는 **시험 항목이 존재함**, **검사 코드가 있음**, **운영 호스트가 실제 실행함**을 구분한다.
설계/프로필의 20행을 생성하는 것만으로 20개를 검사했다고 판단하지 않는다.

실제 출발지 결과는 서로 다른 실행일 수 있으며 만료 시 다시 수집해야 한다. 아래 결과를
합쳐 새로운 full PASS로 만들지 않는다. 실제 온프렘의 개인 접속·시험 주체·등록 원문은 별도
인수 대상이며 AWS의 플랫폼 RKE2를 목적지로 대체하지 않는다.

| 시험 | 검사 코드 | 실제 출발지와 남은 연결 |
|---|---|---|
| SEC-01.1 다른 역할의 Secret 읽기 | `aws_probes.py`, `aws_qualification.py`, `openbao.py` | 두 실제 시험 주체·동일 Secret 인수 필요. 앱 Secret의 메타데이터 확인은 이 시험을 대신하지 않음 |
| SEC-01.2 소스·설정 비밀 탐지 | `secret_scan.py` | [#356](https://github.com/Softbank-Hackathon-Finals/platform/pull/356): 등록 revision 전체 소스·실행 설정 검사. 목적지 산출물은 별도 |
| SEC-01.3 로그 비밀 노출 | [#381](https://github.com/Softbank-Hackathon-Finals/platform/pull/381)의 `secret_logs.py` | 검사·CloudWatch 조회 구현. 승인된 주입·호출·복원 실행부와 전달 완료/양쪽 로그 원천 연결 미구현 |
| SEC-01.4 비밀 회전 후 회복 | `secret_rotation.py` 읽기 전용 판정·격리된 실제 앱/PG 시험 | [회전 시험](secret-rotation.md): 재시작·기존 연결·30초 초과 구분. 운영 Secret/CNPG/풀러 적용·실행권·복구 및 양쪽 접점은 별도 필요 |
| ENC-02.1 Secret 저장 암호화 | `secret_encryption.py`, `target_secret_encryption.py` | #341 실제 AWS 메타데이터. [목적지 상태 수집기](target-secret-encryption.md)와 실제 OpenBao 격리 시험 구현. RKE2 CLI·전체 노드·운영 전달/저장 결속 인수는 별도 |
| ENC-01.1 앱→Pooler TLS | `postgres_tls.py`, `app_process_tls.py` | [#298](https://github.com/Softbank-Hackathon-Finals/platform/pull/298): 출발지 실제 앱 경로. 목적지 실행 필요 |
| ENC-01.2 잘못된 CA/호스트 거부 | 같은 TLS 수집기 | #298 출발지 실제 거부·대조군. 목적지 실행 필요 |
| ENC-01.3 평문 거부 | 같은 TLS 수집기 | #298 출발지 실제 거부·대조군. 목적지 실행 필요 |
| ENC-01.4 복제 TLS | `replication_tls.py` | 고정 SQL 조회·슬롯/sender 결속 구현. T8의 실제 설정 적용 영수증·양쪽 읽기 접점 인수 필요 |
| ENC-01.5 Pooler→DB TLS | `pooler_backend_tls.py` 고정 SQL 수집기 | [실제 PG/PgBouncer 격리 시험](pooler-backend-reader.md): verify-full/verify-ca/평문/재사용/잘못된 CA 구분. 운영 앱·stats-read 주체와 등록·저장 인수 필요 |
| AUD-01.1 관리자 접속 검색 | `audit_probes.py`, T34 수집기 | 실제 CNPG 감사 원천→저장소 전달·최소 권한 검색 인수 필요 |
| AUD-01.2 DDL 검색 | 같은 감사 수집기 | 승인된 앱 DB 시험 스키마와 실제 생성/삭제·중앙 검색 인수 필요 |
| AUD-01.3 권한 변경 검색 | 같은 감사 수집기 | [#369](https://github.com/Softbank-Hackathon-Finals/platform/pull/369): 출발지 별도 시험 namespace 실제 EKS→CloudWatch 부분 검증. 앱 범위·목적지·최소 권한 경로는 별도 |
| NET-01.1 외부 공개 포트 | `public_ports.py` Nmap 스캔·판정 | 등록 주소/포트 범위·실제 외부 실행 주체·허용 기준선 및 대조군 인수 필요 |
| NET-01.2 외부→DB 차단 | `network_probes.py` | 모의·로컬 네트워크 검증. 실제 외부/허용 주체·5432/6432·정책 readback 인수 필요 |
| NET-01.3 다른 namespace→DB 차단 | 같은 네트워크 수집기 | [실제 출발지 시험](../../../docs/evidence/t14-source-network-2026-11-06/README.md): DB5432는 다른 NS에서도 연결. 실제 풀러5432와 계약6432 차이로 전체 UNKNOWN. 방법/등록 정합과 목적지 인수 필요 |
| IAM-01.1 익명 객체 읽기 | AWS 객체 수집기 | #298/#341 실제 출발지. 목적지 SeaweedFS 주소·자격·객체 버전 연결 별도 |
| IAM-01.2 기준선 초과 권한 | `tool_reports.py` 정규화, `prowler_iam.py` 정책 원문·도구 진단 검증 | [실제 Prowler 자격 시험](../../../docs/evidence/t14-prowler-2026-11-06/README.md): 내부 오류 후 PASS 반환을 UNKNOWN으로 차단. 전체 매핑 미확정이며 kube-bench manual/WARN도 PASS로 바꾸지 않음 |
| ISO-01.1 다른 앱의 객체·DB 접근 | 결합된 T14 시험기 없음 | 객체와 DB 모두 허용/거부 주체로 실제 시험해야 함. 익명 객체 시험이나 Secret 시험으로 대체하지 않음 |
| DR-01.1 새 클러스터 복원 | `backup_restore.py`, `cnpg_*`, `postgres_restore.py` | 로컬 자격 경로 있음. T35 양쪽 실제 저장소·archive 준비·표식 경계·신규 복원·검증 인수 필요 |

## 실제 완료를 위해 남은 작업

1. 회전·외부 포트의 검사와 운영 실행 연결을 완료하고, 앱 간 객체/DB 격리 변경을 인수한다.
   실제 Prowler/kube-bench 점검 ID·역할·namespace 매핑도 확정해야 한다.
2. 존재하는 검사기에 승인된 실행 주체·원천·결과 저장소를 연결한다. 운영 설정 변경이 필요한
   시험은 기존 담당의 변경 창과 맞추고 실패/응답 유실 시 복구·정리 근거도 보존한다.
3. T10이 프로필을 고정한 새 운영 계획에 각 실제 실행 원문을 등록한다. T19는 동일 계획·앱·환경
   쌍과 현재 유효한 증거만 비교한다. 목적지 누락은 차단이며 임의 fixture로 채우지 않는다.
4. T21에 부분 완료·UNKNOWN 원인·운영 증거 범위를 표시하고 T22 실제 앱 시연과 연결한다.

T34·T35는 T14의 감사/복구 시험 실행을 공급하고, T36·T37은 관측 판정과 수집 경로를 공급한다.
이 태스크들의 코드 병합을 T14의 실제 양쪽 통과로 계산하지 않는다.
