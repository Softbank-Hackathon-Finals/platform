# T6 출발지 워크로드

새 `source-dev` 개발 네임스페이스에 CNPG PostgreSQL, 전환용 PgBouncer, ESO와 `demo-chat`을 배포한다. EKS 기반과 다른 Terraform state를 사용한다. 실제 검증 결과는 `docs/evidence/t6-runtime-2026-11-03/`에 기록한다. 이 구성은 T8 왕복 복제, T14 통제 비교, T35 백업·복원 완료를 의미하지 않는다.

## 소유권과 입력

기반 담당의 `source-eks` state가 EKS, 기존 IAM OIDC provider, VPC·NAT·노드와 vpc-cni를 소유한다. 이 디렉터리는 **그 객체를 data로 읽기만 한다**. 별도 backend key `runtime/source-workloads/terraform.tfstate`에 EBS CSI add-on/IRSA, 앱·ESO IRSA, Secrets Manager 항목, 첨부/감사 S3, KMS와 CloudTrail을 관리한다. 모든 태그 가능한 AWS 자원에 `component=source`가 붙는다. 기존 팀 예산 설정은 변경하지 않는다.

`account_id`는 기존 foundation의 비공개 출력과 현재 STS 계정을 대조한 값이다. provider의 `allowed_account_ids`로 다른 계정 적용을 거부한다. backend 설정·tfvars·plan·output은 저장소 밖의 개인 디렉터리에만 둔다. 기존 클러스터에 EBS add-on·동일 IAM 이름·operator·namespace가 이미 있으면 소유자를 확인하고 새로 덮어쓰거나 import하지 않는다.

| 구성 | 고정 버전 |
|---|---|
| EKS 기반 | 1.35, 별도 기반 state |
| EBS CSI add-on | `v1.67.2-eksbuild.1` (EKS 1.35 지원 조회) |
| CNPG | 공식 chart `0.29.1`, operator `1.30.1` |
| PostgreSQL | 공식 `18.4-system-trixie`, manifest digest는 `render.py`에 고정 |
| PgBouncer | 공식 CNPG 이미지 `1.25.1`, manifest digest 고정 |
| ESO | 공식 chart/image `2.14.0` |
| demo-chat | 팀 main 소스의 OCI digest를 보존. 정확한 팀 GHCR 또는 같은 AWS 계정·리전의 private ECR `bravo/demo-chat`만 허용 |

[CNPG 지원 표](https://cloudnative-pg.io/docs/1.30/supported_releases/), [CNPG 차트](https://github.com/cloudnative-pg/charts/releases/tag/cloudnative-pg-v0.29.1), [ESO 릴리스](https://github.com/external-secrets/external-secrets/releases/tag/helm-chart-2.14.0)를 확인했다. CNPG 1.28은 이전 사전 준비 문서에서 참고한 버전이며 이번 설치 버전이 아니다.

## 배포 순서

아래의 `$PRIVATE_DIR`는 작업자가 소유한 mode 0700 디렉터리다. `KUBECONFIG`에는 원래 EKS hostname의 TLS 검증을 유지한 private API 접속 설정만 둔다. 토큰·장기 키를 넣지 않는다. 실제 검증은 플랫폼 EC2의 SSM remote-host forwarding + 로컬 operator의 AWS exec 인증을 사용했다. public EKS endpoint나 인바운드 SSH를 열지 않았다.

1. 기반 apply 종료, EKS/nodegroup ACTIVE, 실제 `kubectl get --raw=/readyz` 및 node Ready를 확인한다. 기존 namespace/operator/workload와 운영 데이터가 없는지 읽는다.
2. 이 디렉터리에서 `terraform init -backend-config="$PRIVATE_DIR/workloads.backend.json"`, `terraform validate`, `terraform plan -var-file="$PRIVATE_DIR/workloads.tfvars.json" -out="$PRIVATE_DIR/workloads.tfplan"`을 수행한다. plan은 비공개다. 신규 보조 자원 외 변경이 없음을 확인한 뒤 저장한 plan을 적용한다.
3. `render.py database`의 Namespace 객체로 `source-dev`를 먼저 만든다. 공식 Helm chart를 `cnpg-system`, `external-secrets`에 설치하고 각각 `cnpg-values.yaml`, `eso-values.yaml`을 사용한다. 이미 존재하는 다른 release를 인수하지 않는다.
4. `python3 seed.py db --kubeconfig "$KUBECONFIG"`로 앱·복제 역할의 비밀번호를 생성한다. `render.py database` 출력에 `kubectl apply --server-side --dry-run=server`를 수행한 뒤 실제 적용하고 `Cluster/source-db` Ready를 기다린다.
5. `python3 seed.py pooler --kubeconfig "$KUBECONFIG"`로 **실제 PostgreSQL의 앱 SCRAM verifier**와 별도 제어 자격증명을 연결한다. `render.py pooler`를 server dry-run 후 적용한다. `source-pgbouncer` rollout과 실제 TLS 접속을 확인한다.
6. `seed.py publish --metadata "$PRIVATE_DIR/workload-metadata.private.json" --kubeconfig "$KUBECONFIG"`는 Boto3가 있는 Python에서 실행한다(검증 SDK `boto3==1.40.65`). AWS CLI의 stdin paramfile에 의존하지 않는다. 기존 AWS 값이 다르면 덮어쓰지 않고 실패한다.
7. Terraform `workload_metadata` 출력에 팀 이미지 `app_image`를 더한 metadata로 `render.py application --settings ...`를 실행한다. ECR은 검증 완료된 private registry contract의 exact `repository_url`을 `ecr_repository_url`로 함께 넘긴다. 이 값도 앱 역할 계정·region·정확한 `bravo/demo-chat` 경로와 대조하며 계약 입력이 없으면 ECR을 받지 않는다. 출력에는 IAM ARN 등 비공개 식별자가 있으므로 커밋하지 않는다. server dry-run 후 적용하고 SecretStore/ExternalSecret Ready와 앱 rollout을 확인한다.
8. `python3 verify.py --kubeconfig "$KUBECONFIG" --canary --output "$PRIVATE_DIR/readback.json"`을 실행한다. 감사 로그 전달이 지연되면 이후 `--canary` 없이 다시 읽는다. 성공 종료 자체는 모든 통제의 합격이 아니며 JSON의 개별 관측값을 확인한다.
9. 앱 rollout과 현재 pod의 고정 image/digest를 확인한 뒤, 대상 환경 쓰기가 차단되어 있음을 담당자와 확인한다. `kubectl --kubeconfig "$KUBECONFIG" exec -i -n source-dev deployment/demo-chat -- python - < app_probe.py > "$PRIVATE_DIR/application-readback.json"`으로 합성 HTTP 시험을 수행한다. 실행 전 스크립트 hash와 Git commit을 별도로 기록한다. 결과의 `passed`와 `final_write_blocked`를 모두 확인한다. 시험은 합성 메시지/첨부를 보존하며, 앱이 처음부터 쓰기 차단 상태가 아니면 진행하지 않는다. 통신 실패가 나도 쓰기 차단을 다시 시도하므로 복원 실패 결과는 운영자가 즉시 확인해야 한다.

`render.py`는 렌더링만 한다. `seed.py`는 정해진 namespace의 새 개발 자격증명과 지정된 Secrets Manager version만 다룬다. 비밀번호·SCRAM verifier·관리 토큰은 프로세스 메모리와 stdin으로만 전달하며 파일·출력·Terraform state에 넣지 않는다. Kubernetes Secret 생성에 client-side apply를 쓰지 않아 plaintext `last-applied-configuration`도 남기지 않는다. 기존 Secret의 소유 label이 다르면 중단한다.

이미지 공개 설정은 변경하지 않는다. 비공개 ECR 경로는 현재 `app_role_arn`의 계정과 `region`, 정확한 `bravo/demo-chat` repository 및 digest에 결속한다. 다른 계정/리전·유사 이름·하위 repository는 거부한다. EKS의 private ECR pull은 **기반 노드 역할**을 사용하며 앱 IRSA의 데이터 접근 권한과 구별한다. 기존 EKS add-on도 사용하는 `AmazonEC2ContainerRegistryPullOnly`를 이 state에서 수정하지 않고, 장기 registry credential이나 imagePullSecret을 생성하지 않는다. 이미지 복사는 별도 CI 소유자와 짧은 수명의 Actions OIDC로 수행하며 원본 OCI digest 일치를 확인한 뒤 배포한다. [EKS의 ECR 이미지 사용](https://docs.aws.amazon.com/AmazonECR/latest/userguide/ECR_on_EKS.html)

## DB·앱·전환 계약

| 소비자 | 출발지 계약 |
|---|---|
| T8 | DB `chat`, primary `source-db-rw.source-dev.svc.cluster.local:5432`, publication `source_chat` = `public.attachments`, `public.messages`; 별도 `chat_replicator` 로그인/논리 복제 역할, Kubernetes Secret `source-replication`. HBA는 `replication=database`만 허용하고 물리 복제 접속은 거부 |
| 앱 | `DB_HOST=source-pgbouncer.source-dev.svc.cluster.local`, 5432, `chat_app`; DB 암호는 기존 코드의 `DB_SECRET_ID` → Secrets Manager SDK 조회를 그대로 사용 |
| T12 | PgBouncer transaction 모드. 별도 `cutover_control`은 pooler 관리 DB만 사용하며 PostgreSQL 역할로 만들지 않음. 관리 암호는 control Secrets Manager 항목의 `pgbouncer_password`; PAUSE/RELOAD/RESUME 통합은 T12에서 별도 검증 |
| T34 | EKS `/aws/eks/<cluster>/cluster` audit stream; CNPG primary Pod의 JSON `logger=pgaudit`, `record.audit`; 접속은 PostgreSQL connection record; CloudTrail 전용 S3에 관리 이벤트와 첨부 버킷 data event |

CNPG의 기본 Pooler는 관리 DB가 local peer 인증 전용이므로 전환용 원격 제어 역할을 위해 별도 PgBouncer Deployment를 사용한다. [CNPG pooling](https://cloudnative-pg.io/docs/1.30/connection_pooling/)과 [PgBouncer 설정](https://www.pgbouncer.org/config.html)을 기준으로 backend `verify-full`, CA, client TLS, SCRAM과 전용 관리 역할을 명시했다. CNPG 서버 인증서의 SAN에는 PgBouncer Service DNS도 포함한다. 앱도 `PGSSLMODE=verify-full`과 CA를 사용한다.

앱은 `/readyz`, `/healthz`를 probe로 사용하고 DB 상태는 `/api/health/db`로 따로 확인한다. 처음에는 `WRITE_BLOCKED=true`다. 운영자는 검증된 source만 쓰기를 열어야 하며, 파드 재생성 시 다시 차단되는 이 초기 설정을 T12의 전체 파드 제어와 함께 다룬다. 다른 환경의 쓰기 상태를 확인하지 않은 채 자동으로 해제하지 않는다.

ESO는 namespace SecretStore와 별도 IRSA 역할로 control secret만 읽고 `ADMIN_TOKEN`만 주입한다. 앱 역할은 DB secret 한 개, 첨부 버킷의 List/Get/Put, 해당 KMS key의 서비스 경유 연산만 허용한다. ESO 역할은 DB secret을 읽을 수 없다. 이 경계를 이유 없이 합치지 않는다. [ESO AWS 인증](https://external-secrets.io/v2.14.0/provider/aws-access/)

T8의 다른 환경에서 이 ClusterIP/DNS로 직접 접속할 수 있다고 주장하지 않는다. Tailscale/라우팅·허용 경로, 복제 슬롯과 subscription, 왕복/시퀀스·PK 보정은 T8/T30에서 추가한다. 빈 앱 테이블로 시작했고 실제 앱 검증 뒤 합성 메시지 2개/첨부 1개만 보존했다. 고객 업무 데이터는 없다. 저장소는 encrypted gp3, Retain이고 단일 노드/단일 DB 인스턴스의 개발 구성이다. 백업·복원 및 고가용성은 아직 검증하지 않았다.

PgBouncer 설정은 initContainer가 ConfigMap에서 emptyDir로 복사한다. 단일 파드와
Recreate 전략, `server_login_retry=2`를 사용하며 `/etc/pgbouncer/pgbouncer.ini`만 변경 가능하다.
실행 중 파일 교체·RELOAD 뒤 ConfigMap 영속화, 파드 교체 시 권한 갱신 및 실패 복구는
[T12 연결 계약](../../../docs/contracts/T12-platform-integration.md)을 따른다.

## 감사와 검증

`verify.py --canary`는 `t6_audit_canary` 스키마/시험 테이블과 namespace 전용 `t6-audit-canary` SA·Role·RoleBinding만 만든다. 앱 데이터·암호·사용자 권한을 바꾸지 않는다. PgBouncer의 실제 접속, 잘못된 암호·평문 접속·앱 관리 DB·앱 DDL 거부를 시험한다. DDL 음성 시험은 트랜잭션 안에서 시도하고 허용되더라도 rollback한다.

CloudTrail은 EKS Kubernetes audit를 대신하지 않는다. 두 전달 경로를 따로 확인한다. CNPG는 pgAudit를 JSON으로 구조화하므로 단순 `AUDIT:` 문자열 검색을 성공 기준으로 사용하지 않는다. CloudTrail은 KMS 암호화·로그 검증·multi-region management event와 첨부 버킷 data selector를 사용한다. [CloudTrail KMS 정책](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-kms-key-policy-for-cloudtrail.html)

```sh
python3 -m unittest discover -s scripts/tests -p test_source_workloads.py -v
uvx ruff check --target-version py39 infra/envs/source-workloads scripts/tests/test_source_workloads.py
terraform fmt -check
terraform validate
trivy config --severity HIGH,CRITICAL --exit-code 1 .
```

기존 18개 T6 사전 계약 검사와 연결해 probe/SDK 의존·TLS·역할 분리가 어긋나지 않는지 시험한다. 실제 canary 성공, 로컬 시험, 원격 CI, 팀 리뷰/병합은 각각 별도 증거다. 이 디렉터리는 삭제·destroy 자동화를 실행하지 않는다. 자원 유지/정리는 기반 및 후속 작업 담당과 수명·데이터 보존 범위를 합의한 뒤 진행한다.

## 목적지 DB 이름 해석 준비 (T6 → T12 인계)

출발지 풀러가 목적지로 전환된 뒤에도 `verify-full`을 유지하려면 목적지 DB의 원래 인증서
호스트 이름을 출발지에서 해석할 수 있어야 한다. `pooler_route.target_route_patch()`는 관측한
출발지 Deployment, `target-db-for-pooler` Service, 해당 ProxyGroup의 생성 Service를 받아
목적지 인증서 이름의 `hostAliases`만 추가한다. 별도 `bravo-source-pooler` 경로, 서비스 관계와
준비 상태, 단일 Recreate 풀러, 양쪽 CA 합본을 확인하며 기존에 다른 주소가 있으면 거부한다.
현재 DB 대상 설정·자격·LOGIN은 변경하지 않는다. 적용은 resourceVersion 조건으로 수행하고
Pod 교체 뒤 기존 설정 보존, 실제 풀러 Pod의 TLS 연결과 잘못된 호출자의 거부, named-pod 권한
재결속을 검증한다. Service의 Ready 표시는 네트워크 정책의 실제 거부 시험을 대신하지 않는다.
기본 source manifest를 다시 적용할 때도 이 관측 기반 패치를 반영해야 한다.

이 함수는 T6 인프라 준비용이며 Executor가 자동 호출하지 않는다. 적용 전 현 Deployment·부모 Service·생성 Service를 다시 읽고 반환된 JSON Patch를 검토한다. EKS의 NetworkPolicy 기능과 정확한 PolicyEndpoint가 반영된 뒤 실제 풀러의 정상 TLS → 다른 label 및 namespace 거부 → 정상 TLS를 각각 검증해야 한다.

실측과 첫 실패의 수정 과정은 [연결 증빙](../../../docs/evidence/t12-network-integration-2026-11-04/README.md)에 있다. 비교기·1012 호출·실패 처리·Executor 배포 및 실제 컷오버는 공식 T12 담당과 인수 범위를 맞춘 뒤 진행한다.
