# T16 실제 환경 사전 점검 (2026-11-06)

> **공식 수집이 아니다.** 팀은 연결(T11 · T21 · T32)에 쓰는 공식 T16 수집을 류지환님이 준비 중인 **T7 읽기 전용 수집 역할**로 만들기로 했다. 이 점검은 그 전에 실제 출발지에서 `bravo-inventory`의 결함을 찾으려고 담당자(전동훈) 개인 SSO로 돌린 사전 점검이다. 여기 나온 개수 · 등급을 공식 인벤토리로 인용하지 않는다.

## 무엇을 돌렸나

| 항목 | 값 |
|---|---|
| 앱 소스 | `demo-chat` `f2dbd01034598e7c5268f23a6c32464a3740823e` (`--source-sha`), `--app-path app` |
| 출발지 | AWS 서울 출발지 EKS(비공개 엔드포인트, 플랫폼 호스트 경유 SSM 포트 포워딩), 네임스페이스 `source-dev` |
| 목적지 | 온프렘 목적지 클러스터(Tailscale kube proxy) — 기능 관측용 내보내기만 |
| 프로필 | `services/inventory/profiles/onprem-rke2.json`(`onprem-demo`), `onprem-minimal.json` |
| 실행 | ① `--exports`(Kubernetes 내보내기 + 읽기 전용 호출로 만든 `aws.json`) ② `--exports --aws-live`(Kubernetes 내보내기만, IAM · S3 · Secrets Manager · KMS는 `Boto3Adapter`가 실시간 조회) |
| 권한 | 개인 SSO 관리자 역할. 역할 자체가 넓으므로 읽기 전용 성질은 **코드**(`READ_ONLY_API` 허용 목록과 `inputs.api_calls` 기록)에서 나온다 |

원본 출력(계정 번호 · ARN · 버킷 이름 포함)은 저장소 밖 비공개 경로(디렉터리 `0700`, 파일 `0600`)에 보관했다. 이 문서에는 개수 · 등급 · 해시만 둔다.

| 비공개 원본 | SHA-256 |
|---|---|
| 수정 전 파일 모드 출력 | `7766a0a3758473c56df5120876c1227ffc98d1f9782598975a8521a3c51f06f9` |
| 수정 전 `--aws-live` 출력 | `3e0a6778ec04970bffbe70ef1e39cbbda5c318ad2c80e13161f551ee0a721ea4` |
| 수정 후 파일 모드 출력 | `fdcac854dbd17bdb29b17cfbd3b7235d18c7c5bd4b9f9a341650b5c4698f84c5` |
| 수정 후 `--aws-live` 출력 | `fb7a7190922ee2925b94e63f6459aa63bf1d4c8bcfd1661b7520e871d6d6f873` |

수정 후 `inputs.repo_digest`는 `sha256:371e74da8ddd91b921216fa0f7dc08584369235036d8eaeb13b13768e3640cd5`. 같은 소스를 다른 경로에 복사해 다시 돌려도 `repo_digest`와 문서 `digest`가 같았다.

## 실제와 비교 (수정 후)

실제 값은 같은 세션에서 `kubectl get`과 IAM · S3 · Secrets Manager · KMS 읽기 호출로 따로 확인했다.

| 실제 의존성 | 기대 | 수정 전 | 수정 후 |
|---|---|---|---|
| CNPG 클러스터 1개 (`wal_level=logical`) | `db` · `both` · `config_swap` | 맞음 | 맞음 |
| 첨부 S3 버킷 1개 (앱 IRSA, 비공개, SSE-KMS) | `object_store` · `both` · `config_swap`(코드가 `endpoint_url`을 설정에서 받음) | 등급 맞음. 같은 버킷이 참조 2개로 중복, `region` 없음 | 참조 1개(동작 합침), `public=False`, `region`, `referenced_by_env=S3_BUCKET` |
| 앱 DB 시크릿 (Secrets Manager, 앱이 SDK로 직접 조회) | `secrets` · `both` · `code_change` | 등급 맞음. 회전 · 암호화 키 속성 없음 | `rotation_enabled`, `kms_key_id`(CMK) 채워짐 |
| 제어 시크릿 (ESO SecretStore가 앱 Secret으로 주입) | `secrets` · `deploy` · `migration_omission` | **누락** (Deployment가 쓰지 않는 IRSA 서비스 계정을 따라가지 않음) | 별도 `deploy` 항목, 카탈로그 기본 `code_change` + `agent_needed` |
| 고객 관리 KMS 키 1개 (S3 · Secrets Manager 경유 조건) | `kms` · `deploy` · 기본 `code_change` + `agent_needed` (ADR-0018) | 등급 맞음. 키 상태 속성 없음 | `key_manager=CUSTOMER`, `key_state`, `key_spec` |
| IRSA 역할 2개 (앱, ESO) | `iam` · `both` · `adapter` | 앱 역할만 | 두 역할 모두 |
| 목적지 OpenBao (StatefulSet) | `secret_store` 관측 | `declared: profile`만 | `observed: StatefulSet/…` |

요약(수정 후, 두 실행 모드에서 항목 · 등급 동일): 항목 6개, `config_swap` 2 · `adapter` 1 · `code_change` 3, `agent_needed` 2, `deploy_only` 2, `code_only` 0. 후보: `onprem-demo` = `needs_work`, `onprem-minimal` = `blocked`(`s3_compatible` · `secret_store` 미등록, 각 1줄).

T32 `bravo-recommend`(규칙만)에 수정 후 `--aws-live` 출력을 넣으면 `onprem-demo`를 추천하고 `onprem-minimal`을 제외했다.

이미지 레지스트리(ECR, digest 고정)는 설계대로 항목으로 등급을 매기지 않고 `deploy.workloads`에만 남는다.

## 찾아서 고친 결함

각 결함은 먼저 실패하는 시험을 추가한 뒤 고쳤다. "AI는 `none`을 뒤집지 못한다"와 "환경 변수 값은 복사하지 않는다" 규칙은 그대로다(값 비교는 메모리에서만 하고 출력에는 env **이름**만 남긴다).

| # | 결함 (실측에서 드러난 모습) | 수정 | 시험 |
|---|---|---|---|
| 1 | `app.repo`에 로컬 체크아웃 절대경로(홈 디렉터리)가 들어가고 문서 `digest`에 섞여 체크아웃마다 digest가 달라짐 | `app.repo` = `--repo-ref`(기본 디렉터리 이름) | `test_document_does_not_depend_on_checkout_location` |
| 2 | `repo_digest`가 `app_path`만 해시하는데 설정 파일은 레포 전체를 스캔(루트 `compose.yaml` 변경이 digest에 안 잡힘) | 스캐너가 실제로 읽은 파일 목록으로 해시 | `test_repo_digest_covers_every_scanned_file_not_only_app_path` |
| 3 | `--aws-live`가 `DescribeSecret` · `DescribeKey` · `GetBucketLocation`을 허용 목록에만 두고 호출하지 않음 | 자원마다 한 번씩 조회, 허용 속성만 복사 | `test_boto3_adapter_describes_secrets_keys_and_bucket_location` |
| 4 | Deployment가 쓰지 않는 IRSA 서비스 계정(ESO)을 추적하지 않아 제어 시크릿 누락 | 네임스페이스의 IRSA 서비스 계정 전부 추적 | `test_irsa_service_account_without_a_deployment_is_traced` |
| 5 | `both` 역할 안에 설정이 가리키지 않는 배포 전용 자원이 숨음 | 워크로드 env가 이름으로 가리키지 않는 버킷 · 시크릿 · 키는 별도 `deploy` 항목 | `test_deploy_only_resource_inside_a_code_role_gets_its_own_item`, `test_env_reference_to_a_resource_is_recorded_by_name_only` |
| 6 | 버킷 ARN과 `버킷/*` ARN이 참조 2개로 중복, 같은 버킷을 두 번 조회 | (종류, 이름, 경로) 단위로 합침 | `test_same_resource_named_by_two_arns_is_one_deploy_ref` |
| 7 | `aws.json`이 없어도 `inputs.api_calls`에 `file:aws.json` 기록 | 실제로 읽은 파일만 기록 | `test_file_api_calls_list_only_files_that_were_read` |
| 8 | FastAPI `app.get('/healthz')` 라우트가 환경 변수 참조로 잡힘 | `os.environ` · `env` · `getenv` 수신자만 | `test_route_decorators_and_dict_get_are_not_env_refs` |
| 9 | `.github/workflows`의 `AWS_REGION`이 앱 IAM 설정으로 잡힘 | `.github` 제외 | `test_ci_workflows_are_not_app_configuration` |
| 10 | compose의 moto 시드 컨테이너(`amazon/aws-cli`) env가 앱 설정으로 잡힘 | 에뮬레이터 · 시드 이미지 서비스 범위 제외(앱 env는 유지) | `test_compose_emulator_and_seed_services_are_not_app_configuration` |
| 11 | `AWS_SECRET_ACCESS_KEY`가 Secrets Manager 역할로 분류 | AWS 자격 증명은 `iam`, `AWS_ENDPOINT_URL_<서비스>`는 해당 서비스 역할 | `test_aws_credential_env_is_iam_not_secrets_manager` |
| 12 | 목적지 OpenBao가 StatefulSet이라 관측되지 않음 | `statefulsets.json`도 읽음 | `test_statefulset_workloads_are_observed_as_capabilities` |
| 13 | 한 역할이 항목 2개가 되면 `missing_capabilities`가 같은 줄을 두 번 씀(T32 입력) | 역할당 한 줄 | `test_missing_capability_is_listed_once_per_role` |

## 읽기 전용 확인

`--aws-live` 출력의 `inputs.api_calls`(어댑터가 실제로 부른 목록 전부):

```
file:clusters.json  file:deployments.json  file:serviceaccounts.json
iam:GetRolePolicy  iam:ListAttachedRolePolicies  iam:ListRolePolicies
kms:DescribeKey  s3:GetBucketLocation  s3:GetBucketPolicyStatus  secretsmanager:DescribeSecret
```

모두 `READ_ONLY_API` 안의 Get · List · Describe다. 어댑터의 쓰기 · 허용 목록 밖 호출은 **0건**이다(밖의 이름은 호출 전에 `PermissionError`). 두 역할 모두 관리형 정책이 없어 `iam:GetPolicy` · `iam:GetPolicyVersion`은 호출되지 않았다.

어댑터 밖에서 비교 · 접속을 위해 사람이 실행한 호출도 적는다.

- AWS 읽기: `sts:GetCallerIdentity`, `eks:DescribeCluster`(kubeconfig 작성 포함), 위와 같은 IAM List · Get, `s3:GetBucketPolicyStatus` · `GetBucketLocation` · `GetBucketEncryption`, `secretsmanager:DescribeSecret`, `kms:DescribeKey`, `ssm:DescribeSessions`.
- 세션 제어: 지시된 SSM 포트 포워딩용 `ssm:StartSession` 1회와, 끝난 뒤 남아 있던 **본인** 세션의 `ssm:TerminateSession` 1회. 클라우드 자원을 바꾸는 호출은 없다.
- Kubernetes: `get`만(네임스페이스, Deployment, ServiceAccount, CNPG Cluster · Pooler, StatefulSet, CRD, ExternalSecret · SecretStore). kubeconfig는 비공개 경로에 만들었다가 지웠다.

## 검증하지 못한 것

- T7 읽기 전용 수집 역할로 같은 결과가 나오는지(공식 수집은 그 역할로 다시 만든다).
- 버킷 정책이 없는 버킷에서 `GetBucketPolicyStatus`가 오류를 낼 때의 처리(실제 버킷에는 정책이 있었다. 지금은 `UNKNOWN: <오류 종류>`로 남는다).
- `demo-chat` 작업 트리가 HEAD와 같은지는 git으로 확인하지 못했다(작업 격리 때문에 다른 저장소에 git 명령을 쓸 수 없었다). `--source-sha`는 HEAD 참조 파일에서 읽었다.
- 정책 조건(`kms:ViaService`)은 아직 출력에 옮기지 않는다. KMS 항목의 암묵적 사용 판단은 `agent_needed`로 에이전트에 넘어간다.
