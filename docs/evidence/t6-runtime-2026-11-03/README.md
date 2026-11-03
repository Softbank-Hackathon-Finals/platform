# T6 출발지 실제 구축 검증 · 2026-11-03

사용자가 실제 개발 환경 구축을 요청했고, 기존 기반 배포 담당의 완료·소유권 인계 뒤 별도 `source-workloads` state와 새 `source-dev` namespace에서 작업했다. EKS 기반 state·VPC·NAT·기존 IAM OIDC provider·노드 역할·vpc-cni는 변경하지 않았다. 처음 Kubernetes 조회에서 사용자 namespace와 운영 데이터가 없음을 확인했다.

이 기록은 현재 팀 환경의 실제 AWS/Kubernetes/SQL 관측이다. 개인 B-free나 예선 배포 결과를 재사용하지 않았다. 실행 주체는 기존 개인 IAM operator이며 SSO/다른 팀원의 실제 접속 완료를 주장하지 않는다. 리소스 식별자·원문 로그·자격증명은 내보내지 않고 필요한 상태와 값만 저장했다.

| 범위 | 관측 |
|---|---|
| 관리 경로 | EKS private-only endpoint. 플랫폼 EC2의 SSM remote-host tunnel을 경유하고 로컬 AWS exec 인증으로 `/readyz=ok`, node Ready 1개 확인 |
| EKS | Kubernetes 1.35, nodegroup ACTIVE, control-plane 5종 로그 enabled. 이 상태는 별도 기반 담당의 자원에 대한 읽기 검증 |
| 보조 AWS | 최초 plan 24개 create, 기존 기반 변경/삭제 0. EBS CSI ACTIVE, IRSA 3역할, Secrets Manager 항목 2개, 첨부/감사 버킷, KMS, 전용 CloudTrail |
| CNPG | operator 1.30.1, PostgreSQL 18.4, Ready 1, encrypted gp3 PVC Bound; 단일 인스턴스 개발 구성 |
| PgBouncer | 1.25.1, Ready 1. 실제 `chat_app` 접속, client CA/DNS `verify-full`, DB측 TLSv1.3 조회 성공 |
| DB 권한 | 앱 superuser=false/schema CREATE=false, 실제 앱 DDL 거부. 별도 복제 역할, pooler 제어 역할은 PostgreSQL에 없음 |
| pooler 권한 | 앱의 관리 DB 접근 거부, 별도 제어 역할 관리 접근 허용, 틀린 암호 및 평문 클라이언트 접속 거부 |
| 논리 복제 준비 | `wal_level=logical`, publication `source_chat` = attachments/messages, 앱 테이블 PK 누락 0. 환경 간 subscription/왕복 복제는 미검증 |
| ESO | 별도 service-account IRSA, namespace SecretStore Ready, ExternalSecret SecretSynced. 앱 DB 암호의 직접 Secrets Manager SDK 의존은 유지 |
| DB 감사 | `log_connections=on`, pgAudit `ddl,role`, parameter logging off; 실제 접속 및 구조화된 DDL canary 관측 |
| K8s 감사 | 전용 RoleBinding 카나리가 EKS CloudWatch audit 로그에 실제 관측됨 |
| AWS 감사 | CloudTrail enabled, 전달 오류 없음. S3에 전달된 gzip JSON을 메모리에서 읽어 이번 `PutSecretValue` 2건(08:37:48Z) 확인. KMS 암호화·로그 검증·관리/S3 data selector 설정 |

`database-audit-readback.json`의 관측 시각은 **2026-11-03T08:41:05.531768Z**, `cloudtrail-delivery.json`은 **08:42:24.463076Z**다. DB canary는 이름이 고정된 전용 스키마/시험 테이블이며 업무 테이블 publication에 포함하지 않았다. raw SQL/AWS error를 출력하지 않았다.

초기 EBS PVC는 암호화되어 있었지만 component 태그가 없어 실제 조회로 발견했다. EBS add-on의 공식 configuration schema에 있는 `controller.extraVolumeTags`를 선언해 이후 볼륨에도 비용 태그를 적용하고, 이미 생성한 **자기 namespace의 PVC 볼륨 1개**에만 source/project 태그를 보완·재조회했다. 기반 노드 볼륨은 수정하지 않았다.

로컬 검증은 repository unittest 91개(신규 credential/계약/감사 회귀 8개 포함), 기존 T6 계약 검사 18개, Ruff(py39), Terraform fmt/validate, Trivy config HIGH/CRITICAL, Gitleaks 8.24.3이다. 설치 후 실제 server-side dry-run과 rollout/readback은 별도 수행했다. CI·팀 리뷰·병합 결과는 PR에서 따로 확인한다.

시크릿 게시 첫 시도는 macOS AWS CLI stdin paramfile 처리에서 종료 252로 실패했다. 값은 출력하지 않았고, Boto3의 메모리 호출로 전환해 성공했다. 이미 AWS에 값이 있으면 동일함을 확인해 재사용하며 다르면 덮어쓰지 않는다. 기존 비밀번호를 갱신하거나 새 장기 키를 만들지 않았다.

앱 이미지·API 검증은 후속 기록으로 추가한다. 이 시점의 배포 목록은 PgBouncer만 포함하며 demo-chat 배포 성공을 이 JSON으로 주장하지 않는다. T8의 원격 DB 라우팅·왕복 복제, T12 실제 cutover, T14 통제 비교, T35 백업·복원, 고가용성은 각각 별도 후속 검증이다. source 자원은 요청에 따라 후속 작업을 위해 유지한다.

배포·재검증과 소비 계약: [`infra/envs/source-workloads/README.md`](../../../infra/envs/source-workloads/README.md). 공개 기록은 실제 계정/ARN/endpoint나 자격증명 전달 경로를 포함하지 않는다.
