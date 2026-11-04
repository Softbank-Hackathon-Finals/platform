# T6 출발지 사전 준비

**이 디렉터리의 도구 범위는 로컬 구성 검토다. 도구의 PASS는 실제 배포 결과가 아니다.** 실제 출발지 구현은 [EKS 기반](../source-eks/README.md)과 [워크로드](../source-workloads/README.md), 관측 결과는 [11/3 구축 기록](../../../docs/evidence/t6-runtime-2026-11-03/README.md)과 [11/4 원본 검토·현재 상태 확인](../../../docs/evidence/t6-completion-2026-11-04/README.md)을 따른다. 팀 설계 원본은 [설계 7.2·7.5·8.6·8.10](../../../docs/design.html), 요청 근거는 [T34 작업](../../../docs/tasks.md)이다. T5/T19 원본 대조 게이트의 판정 규칙을 이 도구로 대신하지 않는다.

| 파일 | 역할 |
|---|---|
| `configuration.example.json` | EKS logging 요청과 CNPG·앱·ESO 구성의 검토용 발췌 예시 |
| `preflight.py` | 외부 호출 없이 선택된 구성 요구 18개 검사 |
| `readback.sql` | 배포 후 DB의 설정·확장·역할·publication·PK를 읽기 전용으로 확인 |

```sh
python3 infra/envs/source-aws/preflight.py infra/envs/source-aws/configuration.example.json
python3 -m unittest discover -s scripts/tests -p test_source_preflight.py -v
```

검사 성공은 `configuration_result: PASS`, `scope: local_configuration_only`다. `runtime_verified`와 `deployment_ready`는 항상 `false`이며, 실제로 남은 검증을 반환한다. 실패는 종료 코드 1과 `BLOCK`이다. 입력 원문이나 자격증명을 결과에 출력하지 않는다.

예시는 **배포 가능한 Kubernetes List나 EKS 생성 요청이 아니다**. 이미지·digest, 인스턴스 수·스토리지, selector/Pod 설정, EKS 역할·서브넷, SA IAM 정책·인증서 마운트 등이 빠진 발췌다. 파일을 `kubectl apply` 또는 AWS 쓰기 API에 바로 전달하지 않는다. 전체 manifest/plan의 provider schema 검증도 별도로 필요하다. 이름은 제안 예시이며 실제 배포 이름·팀 채택 계약으로 확정하지 않았다.

## T1에서 받을 입력

현재 foundation 계약은 [`infra/modules/network/aws/outputs.tf`](../../modules/network/aws/outputs.tf)의 `foundation` 출력이다. 새 T6 state가 이 값만 소비하며 T1 state에 EKS를 합치지 않는다.

| 출력 | T6에서 확인할 내용 |
|---|---|
| `account_id`, `region`, `name` | 실행 계정·리전을 readback과 대조하고 기대 계정 외 적용 거부 |
| `vpcs.source.id` | 플랫폼 VPC와 혼동하지 않는 출발지 VPC |
| `subnets.source-0`, `subnets.source-1` | 서로 다른 AZ의 출발지 서브넷 두 개 |
| `security_groups.source_api`, `source_db` | 플랫폼 제어 경로; EKS 노드·파드 정책 전체를 대신하지 않음 |
| `peering_id` | 플랫폼→출발지 제어 경로 확인 |

현재 foundation은 기본 egress를 닫고 피어링의 443/5432만 명시한다. **VPC 존재만으로 EKS 노드 기동과 외부 API 접근이 준비됐다고 볼 수 없다.** EKS/EC2·STS·Secrets Manager·S3/KMS, GHCR/DNS·시간 동기화와 필요한 운영 경로를 먼저 열거하고 NAT/endpoint/프록시 선택·비용·접근 범위를 팀과 정한다. 이번 파일들은 네트워크 정책을 변경하지 않는다. 사람의 SSO/EKS access entry는 T1 접근 준비 산출물을 따르고, 앱·ESO의 workload identity와 구별한다.

11/4에 예선 Railshot·Jasmin의 현재 원격 브랜치 43개와 그 도달 가능한 구현 이력을 조사했다. EKS 리소스·모듈 선언은 검색 범위에서 발견되지 않았고, [Railshot의 고정 원문](https://github.com/Jasmin-Softbank/Railshot/blob/645e28aefa7f3d3996c57ea6853c6e2f67c5bcc4/docs/architecture/self-managed-ingress.md)은 VM 위 K3s를 사용하며 EKS를 전제로 하지 않는다고 명시한다. 따라서 T6의 참고 항목은 예선 구현의 적용 가능성을 검토하고 현재 EKS 모듈의 구조·실환경 근거를 확인하는 것으로 구체화했다. [검색 범위·구조 비교·항목별 완료 근거](../../../docs/evidence/t6-completion-2026-11-04/README.md)를 함께 확인한다. 예선 EKS 모듈을 확보·재사용했다고 표시하지 않으며, 개인 B-free의 구현·실행 결과를 팀 구현으로 대체하지 않는다.

## 앱·DB·시크릿 경계

- **DB 경로:** `demo-chat`의 `DB_HOST/DB_PORT`는 출발지 PgBouncer Service를 가리킨다. PgBouncer의 `chat` DB 항목은 컷오버 전 출발지 CNPG primary Service를 가리킨다. 직접 CNPG를 가리키는 앱 설정, 중복 env, 숨겨진 `envFrom`은 이 검사에서 거부한다. 실제 설정이 ConfigMap 기반이면 검토 전에 해석한 값을 제공한다.
- **SDK 의존 보존:** 팀 `demo-chat/app/db_secret.py`는 `DB_SECRET_ID`로 Secrets Manager를 직접 조회한다. 출발지에서 DB 암호를 ESO로 바꿔 주입하면 S2의 의도된 코드 수정 의존성을 없애므로 이 초안에서는 바꾸지 않는다. ESO 예시는 별도 `ADMIN_TOKEN` 공급에만 연결한다. 앱의 S3·KMS·Secrets Manager 접근은 SA별 최소 IAM 권한과 실제 거부 시험으로 확인해야 한다.
- **운영 probe:** `/healthz`와 `/readyz`를 사용한다. DB 상태를 보는 `/api/health/db`를 readiness로 쓰면 컷오버의 DB 정지 구간에 모든 앱 파드가 트래픽에서 빠질 수 있다. 이 조건은 현재 팀 demo-chat README·config.py를 직접 읽어 반영했다.
- **권한 분리:** 앱 `chat_app`, 논리 복제 역할, PgBouncer 제어 역할을 나눈다. 예시 제어 역할 `cutover_control`은 이름 제안이다. 앱/복제/superuser를 PgBouncer admin에 넣지 않는다. PgBouncer 사용자 인증 자료 생성·회전, 클라이언트 TLS, CA/인증서 마운트, Executor의 PAUSE/RELOAD/RESUME 권한·구성 소유권은 아직 구현하지 않았다.
- **ESO 인증 예시:** namespace `SecretStore` + `auth.jwt.serviceAccountRef`의 IRSA 경로다. SA의 실제 IAM trust와 resource scope는 별도 검증한다. Pod Identity를 채택하면 해당 경로로 예시·검사를 함께 바꿔 검토하며, IRSA가 유일하게 허용된 팀 결정이라고 주장하지 않는다. ([ESO AWS 인증](https://external-secrets.io/latest/provider/aws-access/))

## T8에 전달할 DB 계약과 readback

T8 시작 전에 source primary의 DNS·TLS 이름과 CA, DB 이름, publication 이름·테이블 집합, 복제 역할, 앱·pooler·제어 역할, 허용 경로를 정한다. 자격증명 값은 문서·PR·출력에 넣지 않는다. 현재 `configuration.example.json`의 이름은 로컬 예시다.

`readback.sql`은 `BEGIN READ ONLY` 안에서 `pg_settings`, `pg_extension`, `pg_roles`, publication 목록, PK 없는 public 테이블을 조회하고 `ROLLBACK`한다. 기존 안전한 연결/읽기 역할로 실행하고, 실행 시각·DB/환경 식별자·구성 commit을 비공개 근거에 함께 보존한다. 카탈로그 조회 권한이 없거나 행이 누락되면 확인 불가다. 논리 복제 publication이 존재해도 T8 왕복 복제·지연·PK/시퀀스 보정 성공을 뜻하지 않는다.

CNPG는 `postgresql.parameters`의 `pgaudit.*` 설정을 통해 확장을 관리한다. 기본 WAL 설정에 기대기보다 이 예시는 `wal_level=logical` 의도를 명시한다. 배포 후 실제 설정과 `pending_restart`, 확장 설치 상태를 확인해야 한다. 이미지가 해당 pgAudit 확장을 포함하는지도 버전별로 확인한다. 공식 문서 확인은 CNPG 1.28 기준이며, **이번에 클러스터 버전을 선택하거나 설치하지 않았다.** ([CNPG PostgreSQL 설정](https://cloudnative-pg.io/docs/1.28/postgresql_conf/))

## T34에 전달할 로그 경로와 확인 방법

| 로그 | T6에서 준비할 설정 | 실제 완료를 확인할 증거 |
|---|---|---|
| K8s 감사 | EKS `clusterLogging`의 `audit: enabled` | `/aws/eks/<cluster>/cluster`의 audit stream에 시험 RoleBinding 이벤트가 검색됨 |
| DB 접속 | CNPG `log_connections=on` | 새 시험 연결의 시각·주체를 DB 로그에서 검색 |
| DDL | CNPG `pgaudit.log`에 `ddl` 포함, `pgaudit.log_parameter=off` | 별도 시험 스키마의 무해한 DDL이 검색되고 actor/DB/시각과 연결됨 |
| AWS API | 활성 CloudTrail과 목적에 맞는 event selector | canary API 이벤트·전달 경로·오류 없음 확인; EKS audit와 구분 |

CloudTrail은 EKS Kubernetes 감사 로그를 대신하지 않는다. EKS audit는 CloudWatch로 전달되며 기본 상태에서는 활성화되지 않는다. 로그 설정만 확인한 상태와 canary가 실제 검색된 상태를 구별한다. ([AWS EKS control-plane logging](https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html))

readback 순서는 `eks describe-cluster`의 logging → CloudWatch log group/최근 audit stream → CloudTrail `get-trail-status`와 event selectors → CNPG SQL/Pod 로그다. read-only 수집은 Secret 값·로그 본문 전체를 출력하지 않고 필요한 설정과 시험 이벤트 상관키만 기록한다. CloudTrail management event만으로 S3 객체 data event도 수집된다고 가정하지 않는다.

T34 담당에게는 로그 그룹/DB 로그 수집 위치, retention·암호화 선택, 시험 시각/상관키, 검색용 읽기 권한을 넘긴다. 카나리 생성은 실제 환경에서 쓰기를 발생시키므로 환경·범위를 합의한 뒤 수행한다. 이번에는 계정·클러스터·로그·DB를 조회/변경하지 않았고 SQL도 실행하지 않았다.

## 남은 구축 단계

1. T1 사람 접근·기반 네트워크 완료 및 T6 egress 설계 확인.
2. EKS/operator/Postgres/PgBouncer/ESO 버전과 이미지 digest 고정, S3·KMS·IAM·CloudTrail 포함 Terraform 및 완전한 manifest 작성.
3. 해당 버전의 schema 검증·로컬 동작 확인 뒤 승인된 환경에서 생성 및 readback.
4. T4 앱 digest로 PgBouncer 경유 실제 읽기·쓰기·회전·probe 동작 확인. backend `verify-full`의 CA·DNS 검증은 실제 연결 시험으로 확인. ([PgBouncer TLS 설정](https://www.pgbouncer.org/config.html#server_tls_sslmode))
5. T8 논리 복제·T34 로그 카나리·T14 통제 시험을 연결. 팀 리뷰·CI·병합·이슈 완료 체크는 각각 실제 확인 후 수행.

이 도구는 선택한 설정의 불일치를 빨리 찾는다. Kubernetes schema, 전체 IAM policy, egress/NetworkPolicy, 이미지 취약점, 클라우드 실제 상태, 감사 로그 전달·검색, 데이터 무결성 또는 E2E를 검증하지 않는다.
