# T35 목적지 CNPG 백업 (온프렘 SeaweedFS)

목적지 `target-dev/target-db`의 Barman Cloud 백업을 목적지 SeaweedFS의 전용 버킷에 받는다. 출발지([`source-backup`](../source-backup/README.md))와 같은 구조로, 쓰기 · 복원 신원을 나누고 백업 신원은 지울 수 없게 한다. 이 디렉터리는 등록값과 매니페스트를 렌더링하고 확인만 한다. SeaweedFS · 목적지 클러스터는 바꾸지 않는다.

**아직 적용하지 않았다.** 목적지 SeaweedFS(T9 · T30)와 클러스터(T30)에 적용한 적이 없다.

## 등록 값

| 항목 | 값 | 이유 |
|---|---|---|
| 버킷 · 경로 | `bravo-backup`, Barman 경로 `t35/target-db/` | 첨부 버킷과 섞지 않는다 |
| 쓰기 신원 | `t35-backup-writer`: 그 버킷 Read · Write · List | Barman이 WAL · base backup을 쓰고 확인한다 |
| 복원 신원 | `t35-backup-reader`: 그 버킷 Read · List | 복원 Cluster만 쓴다 |
| 삭제 거부 | 버킷 정책: 두 신원의 객체 · 버전 삭제, 버킷 삭제, 정책 변경 · 삭제, 버전 관리 변경을 Deny | SeaweedFS는 삭제를 Write로 본다. 정책이 없으면 쓰기 신원이 지울 수 있다(#208 실측) |
| 버전 관리 | Enabled(버킷 소유자가 설정) | 관리자 삭제도 이전 버전을 남긴다 |
| 쓰기 저장소 | `target-dev/target-db-backup` ObjectStore, Secret `t35-backup-writer`(`ACCESS_KEY_ID` · `ACCESS_SECRET_KEY`), CA Secret `t35-backup-ca` | 키는 운영자가 메모리에서 만들어 넣는다 |
| 복원 저장소 | `t35-restore-check/target-db-backup-read`, Secret `t35-backup-reader`, `t35-backup-ca` | 원본 namespace와 나눈다 |
| 저장소 접근 라벨 | 인스턴스 · 복구 Pod에 `bravo-object-client=true`(`inheritedMetadata`) | 목적지 SeaweedFS는 이 라벨의 Pod만 받는다 |

다른 신원(첨부용 bootstrap · client)은 이 버킷 권한이 아예 없어 지울 수 없다.

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
2. **확인(관리자 · 쓰기 · 복원 키를 환경 변수로, port-forward 등으로 접속):**

   ```sh
   uv run --project services/control-runner --extra aws python infra/envs/target-backup/verify.py \
     --endpoint https://127.0.0.1:18333 --ca "$PRIVATE_DIR/t35-backup-ca.crt"
   ```

   버전 관리, 정책이 렌더링한 정책과 같은지(SeaweedFS가 원소 하나짜리 목록을 문자열로 돌려줘도 같게 본다), 쓰기 신원의 넣기 · 읽기 · 목록 허용과 삭제 · 버전 삭제 · 버전 관리 중지 · 정책 삭제 · 정책 변경 거부, 복원 신원의 읽기 · 목록 허용과 쓰기 · 삭제 거부를 시험 객체 하나로 본다. 결과 `PASS`(0) · `FAIL`(1) · `UNKNOWN`(2). 시험 객체는 관리자가 지운다.
3. **K8s:** `$R stores` → `kubectl apply --server-side --field-manager=t35-backup -f -`. 그다음 `$R archive-patch`를 `kubectl -n target-dev patch cluster target-db --type merge --patch-file ...`로 적용한다. 플러그인이 붙으며 인스턴스가 한 번 다시 시작된다(단일 인스턴스, 창을 잡는다).
4. **백업:** 출발지와 같다. 실행 ID를 한 번만 정해 `$R backup --run-id <ID>` → `kubectl create -f`. 응답이 유실되면 다시 만들지 말고 이름으로 읽어 UID · `spec.cluster.name` · `bravo.t35/run-id` 주석을 대조한다.
5. **복원:** `$R restore --backup-id <ID> --image <목적지 이미지 태그@digest> --storage-class <SC>`. 복원 Pod도 `bravo-object-client=true`를 받는다. 새 Cluster에서 표식 · 행 수를 비교한다(T14 DR-01 관찰기).

## 검증 (로컬)

| 시험 | 결과 |
|---|---|
| [`qualify_local.sh`](qualify_local.sh) (SeaweedFS 4.52, S3 TLS, `-s3.iam=false`) | **2/2.** 렌더링한 신원 · 정책으로 `verify.py` PASS. 삭제 거부 문을 뺀 정책은 FAIL(쓰기 신원 삭제 성공을 잡음) |
| 같은 저장소에서 따로 잰 허용 · 거부 | 쓰기: 넣기 · 읽기 · 목록 성공, 삭제 · 버전 삭제 · 버전 관리 중지 · 정책 삭제 · 정책 변경 403. 복원: 읽기 · 목록 성공, 넣기 · 삭제 403. 관리자 버전 삭제 성공. 시험 객체 그대로 |
| k3s(CNPG 1.30.1 + Barman CRD 0.15.1) | stores 적용. `inheritedMetadata`만 먼저 패치하면 **재시작 없이 5초 안에** primary Pod에 `bravo-object-client=true`. archive-patch 적용 뒤 기존 필드 유지, Backup · 복원 Cluster server-side dry-run 통과 |
| control-runner `pytest` | `cnpg_backup` Pod 라벨 입력 · 이 렌더러 시험 포함 통과 |

**시험이 찾은 것:** SeaweedFS는 정책의 원소 하나짜리 `Resource` 목록을 문자열로 돌려준다. 그대로 비교하면 같은 정책도 다르다고 나와서 `verify.py`가 목록 · 문자열을 같게 본다.

**재지 않은 것:** 실제 Barman 백업 · 복원(목적지 TLS · 플러그인 준비 뒤), 목적지 NetworkPolicy 변경 뒤 복원 namespace에서의 접근.
