# T35 출발지 CNPG 백업 저장소

출발지 `source-dev/source-db`의 Barman Cloud 백업을 받을 S3 버킷, 전용 KMS 키, 쓰기 · 복원 IRSA 역할 두 개를 만든다. `source-eks`(EKS · OIDC provider)와 `source-workloads`(DB · 앱)는 data로 읽기만 하고 고치지 않는다. 상태는 별도 backend key에 둔다.

**아직 적용하지 않았다.** 실제 계정에서 plan · apply · 백업 · 복원을 돌린 적이 없다. 적용은 출발지 담당(T6)이 아래 순서로 한다. [T35 백업 · 복원 기준](../../../docs/controls/backup-restore.md)을 먼저 읽는다.

## 등록 값 (T6 등록안 #57에 대한 답)

| 항목 | 값 | 이유 |
|---|---|---|
| 버킷 · 경로 | 전용 버킷 `bravo-source-backup-<계정>`, Barman 경로 `t35/source-db/`만 | 첨부 · 감사 버킷과 섞지 않는다 |
| 쓰기 역할 | `source-dev/source-db` ServiceAccount(CNPG가 Cluster 이름으로 만든다)만 신뢰. 그 경로의 Put · Get · 목록 · multipart 중단만 | 삭제 권한은 Barman retention용인데 T35는 retention을 켜지 않는다 |
| 복원 역할 | `t35-restore-check/source-db-restore`만 신뢰. 읽기(Get · 목록 · 복호화)만 | 복원 Cluster 이름이 곧 SA 이름이다. 원본 namespace와 나눈다(변수 검사) |
| 암호화 | 이 스택의 **전용 KMS 키**(회전 켬, 삭제 대기 30일). 앱 키를 쓰지 않는다 | 앱 역할이 앱 키로 백업을 풀 수 없게 한다 |
| 보존 | 현재 객체는 자동으로 지우지 않는다. 이전 버전은 7일(`noncurrent_version_days`), 끝나지 않은 multipart는 1일 | 복원 근거가 사라지지 않게. 데모 뒤 정리는 관리자가 한다 |
| 삭제 · 보호 변경 | **관리자 역할(`custodian_role_arns`, 정확한 SSO 역할 ARN)만.** 나머지는 버킷 정책으로 모두 거부: 객체 · 버전 삭제, 버킷 삭제, 정책 · 버전 관리 · 수명 주기 · 암호화 · 공개 차단 · 소유권 변경 | 앱 · Executor · 노드 역할을 하나하나 나열하지 않아도 거부된다. 적용하는 사람이 관리자 목록에 없으면 plan이 멈춘다(정책을 건 뒤 아무도 못 고치는 상황 방지) |
| 삭제 거부 확인 대상 | 쓰기 SA, 복원 SA(복원 Cluster를 만든 뒤), 앱 `source-dev/demo-chat`, 그리고 `--also-deny`로 Executor 등 다른 SA | `verify.py --delete-probe`가 그 SA로 짧은 Pod를 띄워 시험 객체 삭제를 시도한다 |

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
4. **확인:** `python3 infra/envs/source-backup/verify.py --registration ... --kubeconfig ... --delete-probe --also-deny <Executor namespace>/<SA>`. 설정 읽기(버전 관리, 전용 키, 공개 차단, 소유권, 관리자만 삭제, 역할 신뢰 · 권한)와 시험 객체 삭제 거부. 시험 객체는 관리자가 마지막에 지운다. 실제 백업은 건드리지 않는다.
5. **백업:** 표식 행 기록(기준 문서 "표식과 데이터 경계") → `$R backup` 적용 → `completed`와 backup ID 확인.
6. **복원:** 복원 namespace의 격리(업무 경로 없음, 실제로 강제되는 NetworkPolicy)를 먼저 확인한다. `$R restore --backup-id <ID> --image <원본 이미지 태그@digest> --storage-class <SC>` 적용 → 새 Cluster에서 표식 · 행 수 비교(T14 DR-01 관찰기). `verify.py --delete-probe`를 다시 돌리면 복원 SA도 시험된다.
7. **정리:** 복원 Cluster 삭제는 별도 승인 효과로. 데모 뒤에는 관리자가 버킷을 비우고 `terraform destroy`(`force_destroy=false`라 객체가 남아 있으면 멈춘다).

## 검증 (로컬)

| 시험 | 결과 |
|---|---|
| `terraform test` (모의 provider) | 7개 통과: 관리자 외 삭제 · 보호 변경 거부, 비공개 · 버전 관리 · 전용 키, 역할마다 SA 하나, 적용자가 관리자가 아니면 거부, 와일드카드 관리자 거부, 복원 namespace 분리, 선언 자원 목록 고정 |
| 변이 시험 | 11/11 잡음(쓰기 역할 삭제 허용, 버전 삭제 보호 빠짐, 정책 변경 허용, 복원 역할 쓰기, 버킷 전체 쓰기, 적용자 검사 없음, 다른 키, force_destroy, 버전 관리 중지, 현재 객체 만료, 와일드카드 sub) |
| `trivy config` HIGH · CRITICAL | 0건 |
| k3s(CNPG 1.30.1 + Barman CRD 0.15.1) | stores 적용, role-patch 뒤 기존 SA에 역할 주석 5초 안에 반영, archive-patch가 기존 Cluster 필드를 지우지 않음, Backup · 복원 Cluster server-side dry-run 통과 |
| `verify.py` 판정 | 실제 aws-cli 2.38.0 출력으로 거부 · 자격 없음 · 삭제됨 · 불명 4/4. 흐름은 가짜 aws · kubectl로 7경우 |

**재지 않은 것:** 실제 AWS의 IAM · 버킷 정책 평가, IRSA로 Barman이 S3에 실제로 백업하는지, 플러그인 설치. 앱 역할은 IAM에 이 버킷 권한이 아예 없어서 삭제가 거부되므로, 그 거부가 버킷 정책의 증거는 아니다. 버킷 정책 자체는 `verify.py`의 설정 읽기가 확인한다. 삭제 거부는 Object Lock 같은 불변 보존이 아니다(기준 문서).
