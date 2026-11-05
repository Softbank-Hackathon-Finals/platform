# T35 목적지 CNPG 백업 (온프렘 SeaweedFS)

목적지 `target-dev/target-db`의 Barman Cloud 백업을 목적지 SeaweedFS의 전용 버킷에 받는다. 출발지([`source-backup`](../source-backup/README.md))와 같은 구조로, 쓰기 · 복원 신원을 나누고 백업 신원은 지울 수 없게 한다. 이 디렉터리는 등록값과 매니페스트를 렌더링하고 확인만 한다. SeaweedFS · 목적지 클러스터는 바꾸지 않는다.

**아직 적용하지 않았다.** 목적지 SeaweedFS(T9 · T30)와 클러스터(T30)에 적용한 적이 없다.

## 등록 값

| 항목 | 값 | 이유 |
|---|---|---|
| 버킷 · 경로 | `bravo-backup`, Barman 경로 `t35/target-db/` | 첨부 버킷과 섞지 않는다 |
| 쓰기 신원 | `t35-backup-writer`: 그 버킷 Read · Write · List | Barman이 WAL · base backup을 쓰고 확인한다 |
| 복원 신원 | `t35-backup-reader`: 그 버킷 Read · List | 복원 Cluster만 쓴다 |
| 삭제 거부 | 버킷 정책: **모든 주체(`Principal "*"`)**의 객체 · 버전 삭제, 버킷 삭제, 버전 관리 변경을 Deny. 정책 변경 · 삭제는 버킷 Admin 권한이 있는 소유자 신원만 할 수 있다 | SeaweedFS는 삭제를 Write로 본다. 나중에 앱 · Executor에 Write가 붙어도 지울 수 없게 신원을 나열하지 않는다. 소유자 예외 조건(`ArnNotEquals`)은 SeaweedFS 4.52가 정책 오류로 거부해 쓸 수 없다 |
| 버전 관리 | Enabled(버킷 소유자가 설정) | 관리자 삭제도 이전 버전을 남긴다 |
| 쓰기 저장소 | `target-dev/target-db-backup` ObjectStore, Secret `t35-backup-writer`(`ACCESS_KEY_ID` · `ACCESS_SECRET_KEY`), CA Secret `t35-backup-ca` | 키는 운영자가 메모리에서 만들어 넣는다 |
| 복원 저장소 | `t35-restore-check/target-db-backup-read`, Secret `t35-backup-reader`, `t35-backup-ca` | 원본 namespace와 나눈다 |
| 저장소 접근 라벨 | 인스턴스 · 복구 Pod에 `bravo-object-client=true`(`inheritedMetadata`) | 목적지 SeaweedFS는 이 라벨의 Pod만 받는다 |

**소유자의 정리 절차:** 정책은 소유자도 막는다. 데모 뒤 정리나 버전 관리 변경은 소유자가 정책을 내리고(`DeleteBucketPolicy`) 작업한 뒤 같은 정책을 다시 넣고(`render.py bucket-policy`) 확인한다. 이 창은 기록으로 남긴다.

## 다른 담당에 필요한 것

| 담당 | 필요한 것 | 이유 |
|---|---|---|
| **T9 (SeaweedFS 보안)** | 목적지 SeaweedFS의 **S3 TLS**(`-s3.cert.file` · `-s3.key.file`, 서버 이름 `seaweedfs.target-dev.svc`)와 CA | 백업 경로(`cnpg_backup` SeaweedFS 경로)는 HTTPS · CA를 요구한다. 지금 목적지 SeaweedFS는 평문이다. 같은 인자는 [감사 저장소 시제품](../../../services/control-runner/templates/target-audit-store/store.yaml)에서 확인했다 |
| **T9 · T30** | `target-seaweedfs-auth`의 `s3.json`에 두 신원 추가(`render.py identities`), 관리자로 버킷 생성 · 버전 관리 · 정책(`render.py bucket-policy`) | 신원과 정책은 SeaweedFS 소유자가 적용한다 |
| **T30 (네트워크)** | `target-object-clients` NetworkPolicy에 `t35-restore-check` namespace의 `bravo-object-client=true` Pod 허용 추가 | 지금 정책은 같은 namespace(`target-dev`)의 Pod만 받는다. 복원 Cluster는 다른 namespace다 |
| **T30 (용량)** | volume 상한 상향 또는 백업 전용 인스턴스 | 지금 volume 8개 · 개별 128MB다. WAL은 16MiB 단위라 새 버킷 collection에 쓸 volume이 모자랄 수 있다 |
| **T30 (클러스터)** | cert-manager와 Barman Cloud 플러그인 0.15.1 설치 | 출발지와 같다 |

## 순서

`R="uv run --project services/control-runner python infra/envs/target-backup/render.py"`(저장소 루트).

1. 위 표의 TLS · 신원 · 버킷 · 정책 · NetworkPolicy · 플러그인이 준비됐는지 확인한다.
2. **확인(소유자 · 쓰기 · 복원 · 앱 · Executor 키를 환경 변수 `T35_<LABEL>_AK/SK`로, port-forward 등으로 접속):**

   ```sh
   uv run --project services/control-runner --extra aws python infra/envs/target-backup/verify.py \
     --endpoint https://127.0.0.1:18333 --ca "$PRIVATE_DIR/t35-backup-ca.crt" --deny app --deny executor
   ```

   - **보호부터 읽는다.** 버전 관리가 Enabled이고 정책이 렌더링한 정책과 같아야 한다(SeaweedFS가 원소 하나짜리 목록을 문자열로 돌려줘도 같게 본다). 정책이 없으면 FAIL, 읽지 못하면 UNKNOWN이고 **어느 쪽이든 시험 객체를 쓰지 않는다.**
   - **등록 버킷에서는 보호를 바꿀 수 있는 호출을 하지 않는다.** 시험 객체 하나로 쓰기 신원의 넣기 · 읽기 · 목록, 복원 신원의 읽기 · 목록 · 넣기 거부, 그리고 쓰기 · 복원 · 앱 · Executor 모두의 삭제 · 버전 삭제 거부를 본다. 잘못된 키를 넣어도 버전 관리 · 정책은 그대로다.
   - 버전 관리 중지 · 정책 삭제 · 정책 변경 시도는 `--disposable`(폐기할 시험 버킷)에서만 하고, 끝난 뒤 보호가 그대로인지 다시 읽는다.
   - 앱 · Executor는 필수다. 하나라도 빠지면 결과는 UNKNOWN이고 `unverified`에 남는다. `verified`는 넘긴 라벨이 아니라 **실제 응답**으로 정한다: 그 신원의 모든 호출이 등록대로(쓰기 · 읽기는 허용, 삭제 · 보호 변경은 거부) 답한 경우만 들어간다. 보호를 읽지 못해 멈추면 비어 있고 네 신원 모두 `unverified`다.
   - 시험 객체(3바이트)는 정책 때문에 아무도 지울 수 없어 `<prefix>_t35-probe/` 아래 남고 이름이 출력된다. 소유자가 정리 절차에서 함께 치운다.
   - 결과 `PASS`(0) · `FAIL`(1) · `UNKNOWN`(2).
3. **K8s:** `$R stores` → `kubectl apply --server-side --field-manager=t35-backup -f -`. 그다음 `$R archive-patch`를 `kubectl -n target-dev patch cluster target-db --type merge --patch-file ...`로 적용한다. 플러그인이 붙으며 인스턴스가 한 번 다시 시작된다(단일 인스턴스, 창을 잡는다).
4. **백업:** 출발지와 같다. 실행 ID를 한 번만 정해 `$R backup --run-id <ID>` → `kubectl create -f`. 응답이 유실되면 다시 만들지 말고 이름으로 읽어 UID · `spec.cluster.name` · `bravo.t35/run-id` 주석을 대조한다.
5. **복원:** `$R restore --backup-id <ID> --image <목적지 이미지 태그@digest> --storage-class <SC>`. 복원 Pod도 `bravo-object-client=true`를 받는다. 새 Cluster에서 표식 · 행 수를 비교한다(T14 DR-01 관찰기).

## 검증 (로컬)

| 시험 | 결과 |
|---|---|
| [`qualify_local.sh`](qualify_local.sh) (SeaweedFS 4.52, S3 TLS, `-s3.iam=false`) | **5/5.** ① 렌더링한 정책 + 앱(다른 버킷 권한만) · Executor(이 버킷 Write를 준 합성 신원) + `--disposable`: PASS, Executor 삭제 · 정책 변경 거부. ② 삭제 거부 문을 뺀 정책: FAIL, 시험 객체를 쓰기 전에 멈춤. ③ 정책 없음: FAIL, 시험 객체 없음, 확인한 신원 없음. ④ 쓰기 자리에 소유자 키(잘못된 자격), 등록 버킷 모드: 끝난 뒤 버전 관리 · 정책 그대로. ⑤ 앱 · Executor 없이: UNKNOWN, 미확인 app · executor |
| 같은 저장소에서 따로 잰 것 | `Principal "*"` Deny에서 쓰기 · Executor · 소유자 모두 객체 · 버전 삭제 403, 쓰기 넣기 성공, 버전 관리 중지 403. 정책 변경 · 삭제는 Admin 없는 신원 403. 소유자는 정책 내림 → 삭제 → 정책 복구 가능 |
| k3s(CNPG 1.30.1 + Barman CRD 0.15.1) | stores 적용. `inheritedMetadata`만 먼저 패치하면 **재시작 없이 5초 안에** primary Pod에 `bravo-object-client=true`. archive-patch 적용 뒤 기존 필드 유지, Backup · 복원 Cluster server-side dry-run 통과 |
| control-runner `pytest` | `cnpg_backup` Pod 라벨 입력 · 이 렌더러 시험 포함 통과 |

**시험이 찾은 것:**
- SeaweedFS는 정책의 원소 하나짜리 `Resource` 목록을 문자열로 돌려준다. 그대로 비교하면 같은 정책도 다르다고 나와서 `verify.py`가 목록 · 문자열을 같게 본다.
- `Principal "*"` + `ArnNotEquals aws:PrincipalArn`(소유자 예외)는 `InvalidPolicyDocument`로 거부된다. 그래서 소유자 예외 없이 모두를 막고, 소유자는 정책을 내려 정리한다.
- Read만 있는 신원도 버킷 정책 · 버전 관리를 읽을 수 있다. 읽기로는 잘못 넣은 소유자 키를 가려낼 수 없어서, 등록 버킷에서는 보호를 바꾸는 호출 자체를 하지 않는다.

**재지 않은 것:** 실제 Barman 백업 · 복원(목적지 TLS · 플러그인 준비 뒤), 목적지 NetworkPolicy 변경 뒤 복원 namespace에서의 접근.
