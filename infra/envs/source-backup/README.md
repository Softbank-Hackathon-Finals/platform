# T35 출발지 CNPG 백업 저장소

출발지 `source-dev/source-db`의 Barman Cloud 백업을 받을 S3 버킷, 전용 KMS 키, 쓰기 · 복원 IRSA 역할 두 개를 만든다. `source-eks`(EKS · OIDC provider)와 `source-workloads`(DB · 앱)는 data로 읽기만 하고 고치지 않는다. 상태는 별도 backend key에 둔다.

**2026-11-06 실제 출발지에 1~4단계를 적용했다(T6).** [T35 백업 · 복원 기준](../../../docs/controls/backup-restore.md)을 먼저 읽는다.

- 스택: plan 13 추가 · 0 변경 · 0 삭제를 그대로 적용했고, 적용 뒤 plan의 drift는 0이다.
- 사전 조건: cert-manager v1.19.1과 Barman Cloud 플러그인 0.15.1을 공식 릴리스 manifest로 설치했다. ObjectStore CRD와 플러그인 인증서가 Ready다.
- K8s: ObjectStore 두 개와 복원 namespace를 만들었다. role-patch 뒤 `source-db` SA에 쓰기 역할 주석이 붙은 것을 확인했고, 이어 archive-patch를 적용했다. DB Pod는 21:42:25–21:42:55 KST(약 30초) 동안 다시 시작됐다. Cluster `ContinuousArchiving=True`이며, WAL이 `t35/source-db/` 아래에 5분 간격으로 기록된다.
- 확인(`--stage before-restore`): 설정 읽기 9개 항목 통과. 삭제 시험은 쓰기·앱 DENIED, 시험 객체 버전 보존, 관리자가 시험 객체 삭제. 복원 SA는 아직 없다. Executor는 `UNKNOWN_NO_CREDENTIALS`로 판정이 `UNKNOWN`이다. 출발지 노드는 IMDSv2 hop limit 1이라 IRSA가 없는 Pod가 노드 역할 자격을 받지 못한다. 즉 Executor는 AWS 신원 자체가 없다. 아래 4단계의 "노드 역할로 나간다"는 이 클러스터에는 맞지 않는다. 판정 규칙은 완화하지 않았다.
- **T35 결정(11/8):** Executor에 삭제 거부를 보이려고 AWS 역할을 새로 주지 않는다. AWS를 쓰지 않는 구성요소에 신원을 더하면 공격 면만 늘어난다. 대신 "AWS 신원이 없음"을 시험으로 보이는 `--executor-without-aws`로 등록한다(4단계). 판정 기준은 그대로이고, `NO_AWS_IDENTITY`는 Executor에만 인정한다.
- 5단계(표식 · 백업)부터는 T35 담당이 이어간다. 실제 백업 · 복원은 아직 실행하지 않았다. 출발지 DB에 표식을 쓰고 같은 EKS에 복원 Cluster를 만드므로 해피패스 실행(S1~S3)과 겹치지 않는 창에서 한다.

## 등록 값 (T6 등록안 #57에 대한 답)

| 항목 | 값 | 이유 |
|---|---|---|
| 버킷 · 경로 | 전용 버킷 `bravo-source-backup-<계정>`, Barman 경로 `t35/source-db/`만 | 첨부 · 감사 버킷과 섞지 않는다 |
| 쓰기 역할 | `source-dev/source-db` ServiceAccount(CNPG가 Cluster 이름으로 만든다)만 신뢰. 그 경로의 Put · Get · 목록 · multipart 중단만 | 삭제 권한은 Barman retention용인데 T35는 retention을 켜지 않는다 |
| 복원 역할 | `t35-restore-check/source-db-restore`만 신뢰. 읽기(Get · 목록 · 복호화)만 | 복원 Cluster 이름이 곧 SA 이름이다. 원본 namespace와 나눈다(변수 검사) |
| 암호화 | 이 스택의 **전용 KMS 키**(회전 켬, 삭제 대기 30일). 앱 키를 쓰지 않는다 | 앱 역할이 앱 키로 백업을 풀 수 없게 한다 |
| 보존 | 현재 객체는 자동으로 지우지 않는다. 이전 버전은 7일(`noncurrent_version_days`), 끝나지 않은 multipart는 1일 | 복원 근거가 사라지지 않게. 데모 뒤 정리는 관리자가 한다 |
| 삭제 · 보호 변경 | **관리자 역할(`custodian_role_arns`, 정확한 SSO 역할 ARN)만.** 나머지는 버킷 정책으로 모두 거부: 객체 · 버전 삭제, 버킷 삭제, 정책 · 버전 관리 · 수명 주기 · 암호화 · 공개 차단 · 소유권 변경 | 앱 · Executor · 노드 역할을 하나하나 나열하지 않아도 거부된다. 적용하는 사람이 관리자 목록에 없으면 plan이 멈춘다(정책을 건 뒤 아무도 못 고치는 상황 방지) |
| 삭제 거부 확인 대상 | 쓰기 SA, 복원 SA(복원 Cluster를 만든 뒤), 앱 `source-dev/demo-chat`, Executor(`--executor`, 필수), 그 밖의 SA(`--also-deny`) | `verify.py --delete-probe`가 그 SA로 짧은 Pod를 띄워 **실제 AWS 주체가 등록 역할인지 확인한 뒤** 시험 객체 삭제를 시도한다 |

**관리자 후보:** 적용하는 T6 담당과 T35 담당의 SSO 역할. 사람마다 SSO 역할 ARN(경로 포함)을 그대로 넣는다.

## 순서

`PRIVATE_DIR`은 저장소 밖 개인 디렉터리다. backend 설정 · tfvars · plan · output을 git에 넣지 않는다.

1. **사전 조건(T6):** 출발지 클러스터에 cert-manager와 Barman Cloud 플러그인 0.15.1. 지금 출발지에는 ObjectStore CRD가 없다(#57 확인).
2. **스택 적용(관리자 역할로):**

   ```sh
   terraform init -backend-config="$PRIVATE_DIR/backup.backend.json"
   terraform plan -var-file="$PRIVATE_DIR/backup.tfvars" -out="$PRIVATE_DIR/backup.tfplan"   # operator.tfvars.example 형식
   terraform apply "$PRIVATE_DIR/backup.tfplan"
   terraform output -json backup_registration > "$PRIVATE_DIR/backup-registration.json"
   ```

3. **K8s 렌더링 · 적용:** 저장소 루트에서 `R="uv run --project services/control-runner python infra/envs/source-backup/render.py --registration $PRIVATE_DIR/backup-registration.json"`.
   1. `$R stores` → `kubectl apply --server-side --field-manager=t35-backup -f -`: 쓰기 ObjectStore(`source-dev`), 복원 namespace, 읽기 ObjectStore.
   2. `$R role-patch` → `kubectl -n source-dev patch cluster source-db --type merge --patch-file ...` → **`source-db` SA에 `eks.amazonaws.com/role-arn` 주석이 붙었는지 확인.**
   3. `$R archive-patch` → 같은 merge patch. 플러그인 사이드카가 붙으며 **인스턴스 Pod가 다시 시작된다**(단일 인스턴스라 짧은 중단, 창을 잡는다). IRSA 자격은 Pod가 생길 때만 주입되므로 2를 먼저 한다.
4. **확인:**

   ```sh
   python3 infra/envs/source-backup/verify.py --registration "$PRIVATE_DIR/backup-registration.json" \
     --kubeconfig "$KUBECONFIG" --delete-probe --stage before-restore \
     --app-role-arn "$APP_ROLE_ARN" --executor-without-aws "<Executor namespace>/<SA>"
   # Executor에 AWS 역할이 있는 클러스터라면 대신: --executor "<namespace>/<SA>=<그 Pod가 쓰는 역할 ARN>"
   ```

   - **설정 읽기:** 버전 관리, 전용 키, 공개 차단, 소유권, 역할 신뢰 · 권한. 버킷 정책은 스택의 세 Deny 문과 **정확히** 같아야 한다(두 종류의 삭제 · 보호 변경 Action, 버킷 · 객체 Resource, Principal `*`, 관리자 예외, TLS 거부). Action을 줄이거나 Resource를 바꾼 정책, Allow 문이 더해진 정책은 FAIL이다.
   - **삭제 시험:** 각 Pod가 `sts get-caller-identity`로 쓴 역할이 등록 역할과 같고, 두 삭제가 각각 AccessDenied일 때만 그 신원을 확인한 것으로 센다. SA 미배치 · 조회 실패 · 자격 없음 · 역할 불일치 · Pod 생성 실패는 UNKNOWN이다. **쓰기 · 앱 · Executor는 필수다.** Executor 등록(`--executor` 또는 `--executor-without-aws`)이 없거나 그 거부가 확인되지 않으면 PARTIAL · PASS가 아니라 UNKNOWN이다. 그 밖의 신원은 `--also-deny`로 더한다.
   - **AWS 신원이 없는 Executor(`--executor-without-aws`):** 아래를 모두 확인할 때만 `NO_AWS_IDENTITY`(거부로 인정)다. 하나라도 아니면 UNKNOWN이고, 삭제되면 FAIL이다.
     1. 그 SA로 실행 중인 Executor Pod가 하나 이상 있고, 어느 컨테이너에도 AWS 자격 출처가 없다. 자격 환경변수(`AWS_ACCESS_KEY_ID` · `AWS_ROLE_ARN` · `AWS_CONTAINER_CREDENTIALS_*` · `AWS_PROFILE` 등), 키를 알 수 없는 `envFrom`, `.aws` 마운트, `sts.amazonaws.com` · `pods.eks.amazonaws.com` 대상 projected token, `hostNetwork`(노드 메타데이터가 한 홉)를 본다. IRSA · Pod Identity가 붙으면 웹훅이 이 환경변수와 token을 넣으므로 여기서 걸린다.
     2. 그 Pod들이 도는 노드마다 EC2 메타데이터가 꺼져 있거나 IMDSv2 필수 · hop limit 1이다(`providerID` → `ec2 describe-instances`).
     3. 같은 SA의 시험 Pod를 **그 노드에 고정해** 띄우면, 주체 조회가 비고 두 삭제가 모두 `Unable to locate credentials`로 실패한다. 네트워크 오류나 AccessDenied는 "자격 없음"이 아니므로 UNKNOWN이다.
     - 이미지 안에 넣은 자격 파일은 이 시험으로 보지 않는다(이미지 비밀 검사의 범위). 확인은 그 시점의 상태이므로 Executor 배포나 노드 그룹을 바꾸면 다시 돌린다.
   - **결과:** `PASS`(0), `FAIL`(1, 삭제됨 · 설정 다름), `UNKNOWN`(2), `PARTIAL`(3, `--stage before-restore`에서 복원 SA만 아직 없음). 복원 Cluster를 만든 뒤 `--stage final`로 다시 돌려 `PASS`를 받는다.
   - 시험 객체는 관리자가 마지막에 지운다. 실제 백업은 건드리지 않는다.
5. **백업:** 표식 행 기록(기준 문서 "표식과 데이터 경계") 뒤, 이번 실행의 ID를 **한 번만** 정해 저장한다.

   ```sh
   echo "r$(date -u +%Y%m%d%H%M)-$(openssl rand -hex 3)" > "$PRIVATE_DIR/t35-run-id"   # 이미 있으면 다시 만들지 않는다
   $R backup --run-id "$(cat "$PRIVATE_DIR/t35-run-id")" > "$PRIVATE_DIR/t35-backup.json"
   kubectl create -f "$PRIVATE_DIR/t35-backup.json"
   ```

   이름(`source-db-t35-<실행 ID>`)과 `bravo.t35/run-id` 주석이 실행에 묶인다. `create`라서 같은 이름이 이미 있으면 덮어쓰지 않고 실패한다. **응답을 못 받았으면 다시 만들지 말고** `kubectl -n source-dev get backup <이름> -o json`으로 UID · `spec.cluster.name` · 주석을 대조해 이번 실행의 Backup인지 확인한다. 그 뒤 `completed`와 backup ID를 기록한다.
6. **복원:** 복원 namespace의 격리(업무 경로 없음, 실제로 강제되는 NetworkPolicy)를 먼저 확인한다. `$R restore --backup-id <ID> --image <원본 이미지 태그@digest> --storage-class <SC>` 적용 → 새 Cluster에서 표식 · 행 수 비교(T14 DR-01 관찰기). `verify.py --delete-probe --stage final`로 복원 SA까지 확인해 `PASS`를 받는다.
7. **정리:** 복원 Cluster 삭제는 별도 승인 효과로. 데모 뒤에는 관리자가 버킷을 비우고 `terraform destroy`(`force_destroy=false`라 객체가 남아 있으면 멈춘다).

## 검증 (로컬)

| 시험 | 결과 |
|---|---|
| `terraform test` (모의 provider) | 7개 통과: 관리자 외 삭제 · 보호 변경 거부, 비공개 · 버전 관리 · 전용 키, 역할마다 SA 하나, 적용자가 관리자가 아니면 거부, 와일드카드 관리자 거부, 복원 namespace 분리, 선언 자원 목록 고정 |
| 변이 시험 | 11/11 잡음(쓰기 역할 삭제 허용, 버전 삭제 보호 빠짐, 정책 변경 허용, 복원 역할 쓰기, 버킷 전체 쓰기, 적용자 검사 없음, 다른 키, force_destroy, 버전 관리 중지, 현재 객체 만료, 와일드카드 sub) |
| `trivy config` HIGH · CRITICAL | 0건 |
| k3s(CNPG 1.30.1 + Barman CRD 0.15.1) | stores 적용, role-patch 뒤 기존 SA에 역할 주석 5초 안에 반영, archive-patch가 기존 Cluster 필드를 지우지 않음, Backup · 복원 Cluster server-side dry-run 통과 |
| `verify.py` 판정 | `scripts/tests/test_source_backup_verify.py` 7개: 스택 정책만 통과(Action 축소 · Resource 치환 · Allow 추가 · 관리자 변경 · Sid 변경 등 13가지 거부), 등록 역할 · AccessDenied 2회만 확인, SA 조회 금지(Forbidden)는 UNKNOWN, 복원 전 PARTIAL. 변이 13/13. 삭제 메시지 형식은 실제 aws-cli 2.38.0 출력 |
| `--executor-without-aws` | 같은 파일 6개 더: Executor 노드에 고정한 시험 Pod, `Unable to locate credentials` 2회만 인정, Pod의 자격 출처 7종(키 · IRSA · Pod Identity · hostNetwork · envFrom · token · `.aws` 마운트)은 시험 Pod 전에 UNKNOWN, 메타데이터 v1 허용 · hop 2 · 노드 식별 불가 · 인스턴스 중복은 UNKNOWN, `NO_AWS_IDENTITY`는 Executor에만. 변이 13/13. **실환경에서는 아직 돌리지 않았다** |

**재지 않은 것:** 실제 AWS의 IAM · 버킷 정책 평가, IRSA로 Barman이 S3에 실제로 백업하는지, 플러그인 설치. 앱 역할은 IAM에 이 버킷 권한이 아예 없어서 삭제가 거부되므로, 그 거부가 버킷 정책의 증거는 아니다. 버킷 정책 자체는 `verify.py`의 설정 읽기가 확인한다. 삭제 거부는 Object Lock 같은 불변 보존이 아니다(기준 문서).
