# T8 첨부 복사 Job (컷오버 직전 1회)

컷오버 직전에 첨부를 S3에서 SeaweedFS로 한 번 복사하고, 그 결과를 성공 · 실패 · 미확인으로 판정한다. 복사 로직은 기존 [`attachment_copy.py`](../../infra/envs/target-workloads/attachment_copy.py)를 그대로 쓰고 별도 서비스는 없다. 실제 AWS · 클러스터 · VM에는 아직 적용하지 않았다.

| 파일 | 역할 |
| --- | --- |
| [`attachment_job.py`](../../infra/envs/target-workloads/attachment_job.py) | Job 매니페스트와 S3 세션 정책을 출력한다. 아무 데도 접속하지 않는다 |
| [`attachment_verdict.py`](../../infra/envs/target-workloads/attachment_verdict.py) | Job · Pod · 로그를 읽어 판정한다. `kubectl get`과 `logs`만 쓴다 |
| [`attachment-copy.Dockerfile`](../../infra/envs/target-workloads/attachment-copy.Dockerfile) | 복사 모듈과 해시 고정 의존성을 담은 이미지. 자격은 넣지 않는다 |

## 실행 순서

`$PRIVATE_DIR`은 저장소 밖 개인 디렉터리다. 값은 커밋하지 않는다.

1. **업로드를 멈춘다.** 이 Job은 시작 뒤에 들어온 업로드를 알 수 없다.
2. **이번 컷오버와 묶을 값을 정한다.** `PLAN_ID`와 `EPOCH`는 컷오버 요청에 넣을 값과 같아야 한다. `RUN_ID`는 복사 시도마다 새 값이다. 소문자 · 숫자 · 하이픈, 47자 이하이고, 같은 이름의 Job은 다시 만들 수 없다.

   ```sh
   export PLAN_ID=<컷오버 요청의 plan_id> EPOCH=<컷오버 요청의 epoch> RUN_ID=<복사 시도 ID>
   ```

3. **이미지를 준비한다.** 빌드 후 대상 노드에 넣고 digest로 고정한다. 레지스트리 게시 경로는 아직 없으므로 [이미지 전달 절차](target-image-handoff.md)처럼 내보내고 가져온다.

   ```sh
   docker buildx build --platform linux/amd64 -t attachment-copy:"$RUN_ID" \
     -f infra/envs/target-workloads/attachment-copy.Dockerfile infra/envs/target-workloads
   export IMAGE=<저장소>@sha256:<가져온 이미지의 digest>
   ```

4. **DB 경로를 확인한다.** `target-dev/source-db-for-replication`은 ExternalName이라 주소가 없다. operator가 만든 ClusterIP Service의 주소와 UID를 읽는다. 결과는 정확히 한 줄이어야 하고, 값이 바뀌면 다시 렌더한다.

   ```sh
   kubectl -n tailscale-system get service -o jsonpath='{range .items[*]}{.spec.clusterIP} {.metadata.uid}{"\n"}{end}' \
     -l tailscale.com/parent-resource=source-db-for-replication,tailscale.com/parent-resource-ns=target-dev,tailscale.com/proxy-group=bravo-target-replication
   ```

5. **출발지 S3 세션을 Secret으로 만든다.** 30분짜리 세션이고 파일 · 출력에 남지 않는다. `SESSION_POLICY`는 버킷과 앱 KMS 키만 허용한다. 두 ARN은 `source-workloads`의 비공개 출력과 KMS alias에서 얻는다.

   ```sh
   export SESSION_POLICY="$(python3 infra/envs/target-workloads/attachment_job.py source-policy \
     --bucket-arn "$SOURCE_BUCKET_ARN" --kms-key-arn "$SOURCE_KMS_KEY_ARN")"
   ```

   <!-- source-secret-recipe -->
   ```sh
   aws sts get-federation-token --name t8-attachment-copy --duration-seconds 1800 \
     --policy "$SESSION_POLICY" --output json \
     | jq -r --arg bucket "$SOURCE_BUCKET" --arg region "$SOURCE_REGION" --arg owner "$SOURCE_BUCKET_OWNER" '
         "bucket=\($bucket)", "region=\($region)", "bucket_owner=\($owner)",
         "aws_access_key_id=\(.Credentials.AccessKeyId)",
         "aws_secret_access_key=\(.Credentials.SecretAccessKey)",
         "aws_session_token=\(.Credentials.SessionToken)"' \
     | kubectl -n target-dev create secret generic attachment-copy-source --from-env-file=/dev/stdin
   ```

6. **Job을 렌더해서 시작한다.** 준비 확인용 객체처럼 DB 밖의 객체가 출발지에 있으면 키를 하나씩 확인한 뒤 `attachment-copy-approved-extras` Secret의 `keys`에 한 줄씩 넣고 `--with-approved-extras`를 붙인다. 기본은 설명 안 되는 객체를 거부한다.

   ```sh
   python3 infra/envs/target-workloads/attachment_job.py job --image "$IMAGE" --run-id "$RUN_ID" \
     --plan-id "$PLAN_ID" --epoch "$EPOCH" --egress-address "$EGRESS_ADDRESS" --egress-service-uid "$EGRESS_SERVICE_UID" \
     > "$PRIVATE_DIR/attachment-job.json"
   kubectl create -f "$PRIVATE_DIR/attachment-job.json"
   for _ in $(seq 130); do
     kubectl -n target-dev get job "attachment-copy-$RUN_ID" \
       -o jsonpath='{.status.conditions[?(@.status=="True")].type}' | grep -Eq 'Complete|Failed' && break
     sleep 5
   done
   ```

   Job은 10분 안에 끝나거나 시간 초과로 실패한다. 이 대기가 먼저 끝나거나 kubectl이 실패해도 다음 단계로 간다. 그때 판정은 `UNKNOWN`이다.

7. **판정을 읽는다.** 종료 코드와 JSON이 결과다.

   ```sh
   python3 infra/envs/target-workloads/attachment_verdict.py --run-id "$RUN_ID" \
     --plan-id "$PLAN_ID" --epoch "$EPOCH" --image "$IMAGE" > "$PRIVATE_DIR/attachment-result.json"
   ```

8. **Secret을 지운다.** 지우지 않아도 세션은 30분 뒤 끝나지만, 승인 목록의 객체 키는 남는다. 없는 Secret은 무시한다.

   ```sh
   kubectl -n target-dev delete secret attachment-copy-source attachment-copy-approved-extras --ignore-not-found
   ```

**재실행:** 8번까지 마친 뒤 새 `RUN_ID`로 5번부터 다시 한다. 세션을 새로 받아야 복사 도중에 만료되지 않는다.

## 판정

| 판정 | 종료 코드 | 조건 | 다음 행동 |
| --- | --- | --- | --- |
| `SUCCEEDED` | 0 | Job 완료, Pod 한 개, 고정 digest, 형식이 맞고 숫자가 맞는 성공 영수증이 모두 일치 | 컷오버 조건 하나를 충족 |
| `FAILED` | 1 | Job 실패, 복사 코드가 허용 목록의 실패 사유를 남김 | [실패 사유 표](../../infra/envs/target-workloads/README.md#영수증과-실패)대로 고친 뒤 새 실행 ID로 재실행 |
| `UNKNOWN` | 2 | 그 밖의 전부. 로그 없음, Pod 정리됨, 강제 종료, 형식 불일치, Job과 영수증의 불일치 | 성공으로 세지 않는다. `reason_code`를 보고 새 실행 ID로 재실행한다. 이미 복사한 객체는 `reused`로 넘어간다 |

| `reason_code` | 뜻 |
| --- | --- |
| `receipt_passed`, `receipt_failed` | 영수증을 믿을 수 있다 |
| `job_unreadable` | Job을 읽지 못했다. 없거나 정리됐거나 접속 실패 |
| `job_not_as_rendered` | 다시 렌더한 값과 이름 · 라벨 · 계획 · epoch · 이미지 · 명령 · 환경 변수 · 재시도 설정이 다르다. 환경 변수로 대역 주소를 넣은 실행은 여기에 걸린다. 볼륨과 `hostAliases`는 비교하지 않으므로 실수를 막는 검사이고 악의적인 조작을 막지는 못한다 |
| `job_not_finished`, `job_conditions_conflict` | 아직 진행 중이거나 Job 상태가 모순된다 |
| `pod_count_not_one`, `pod_not_as_rendered`, `pod_not_terminated` | Pod가 정리됐거나 둘 이상이거나, 다른 Job · 이미지의 것이거나, 복사 컨테이너가 끝나지 않았다. 목적지에 끝내 닿지 못하면 복사가 시작되지 않아 여기에 걸린다 |
| `image_digest_mismatch` | 런타임이 보고한 이미지 digest가 다르다 |
| `receipt_unreadable`, `receipt_invalid` | 로그가 없거나, JSON 한 줄이 아니거나, 필드 · 숫자가 맞지 않는다 |
| `job_and_receipt_disagree` | Job은 성공인데 영수증은 실패처럼, 서로 다르게 말한다 |

원문 로그와 kubectl 오류는 결과에 넣지 않는다. 객체 키나 주소가 섞일 수 있어서다. stderr도 같은 이유로 Job 명령에서 버리므로, 가져오기 실패 같은 충돌은 빈 로그와 종료 코드만 남고 `UNKNOWN`이 된다. Job은 끝난 뒤 24시간이 지나면 정리되고, 그 뒤의 판정은 `UNKNOWN`이다.

## 필요한 권한

| 대상 | 필요한 것 | 주체와 전달 | 상태 |
| --- | --- | --- | --- |
| 출발지 S3 | 버킷 `s3:ListBucket`, 객체 `s3:GetObject`, S3 경유 `kms:Decrypt`. 그 밖은 명시적으로 거부 | 단기 세션, Secret `attachment-copy-source`의 6개 키 | **새 Secret.** IAM 변경 없음. 세션 권한은 운영자 권한과 세션 정책의 교집합이므로 운영자에게 이 권한이 있어야 한다(미확인) |
| 목적지 SeaweedFS | 정확한 버킷의 Read · Write · List. 버킷 생성은 못 한다 | `client` identity, Secret `target-object-client` | 기존 |
| 출발지 DB | `public.attachments` SELECT. 코드가 읽기 전용 트랜잭션을 건다 | `chat_replicator`, Secret `t8-source-replication`, CA `t8-source-ca` | 기존. 역할이 `messages` SELECT와 복제 권한도 가져 필요보다 넓다 |
| 네트워크 | Pod 라벨 `bravo-object-client=true`, tailnet `bravo-target-replication` → `bravo-source-db` TCP 5432, 대상 → S3 HTTPS | 렌더 결과 · 기존 ACL · 대상 VM의 인터넷 경로 | 라벨은 렌더됨. S3 HTTPS 경로는 미확인 |
| 쿠버네티스 | Pod에는 토큰을 주지 않는다. 운영자는 `target-dev`의 secrets create/delete, jobs create/get, pods get/list, pods/log get | 운영자 개인 계정 | 기존 관리자 접근 |

**새 IAM 코드를 추가하지 않았다.** [`target_registry_credential.py`](../../scripts/target_registry_credential.py)와 같은 방식(운영자 IAM 사용자의 페더레이션 세션 + 세션 정책)이면 AWS에 남는 자원이 없고, 장기 키도 만들지 않으며, T6 state도 바꾸지 않는다. 운영자가 SSO 역할뿐이라면 `GetFederationToken`을 쓸 수 없다. 그때는 [`source-backup`](../../infra/envs/source-backup/README.md)처럼 별도 state에 읽기 역할 하나를 추가할 수 있고, 그 역할을 맡는 명령만 위 첫 줄에서 바뀐다.

세션 토큰 키는 필수다. 장기 키만 넣으면 컨테이너가 시작하지 않는다(`CreateContainerConfigError`). 이 Pod 전용 egress NetworkPolicy는 렌더하지 않았다. 프록시의 Pod · 포트를 확인한 뒤에만 쓸 수 있고, S3는 FQDN으로 좁힐 수 없다.

복사 전에 `wait-for-target` init 컨테이너가 SeaweedFS 연결을 최대 약 2분 기다린다. k3s에서 새 Pod의 연결은 시작 후 약 1초 동안 거부됐고, 복사 코드는 재시도 없이 그 안에 첫 요청을 보내 매번 실패했다.

## T12가 읽을 결과 형식 (제안)

호출 형식은 배규태 님과 맞춘다. 두 방식 모두 결과 JSON은 같다.

**추천은 A다.** Executor를 바꾸지 않고 시작할 수 있고, 나중에 B로 옮겨도 T12의 검사는 그대로다.

| 방식 | 호출 | T12에 필요한 것 |
| --- | --- | --- |
| **A. 운영자 실행 후 결과 전달** | 위 순서를 실행하고 `attachment-result.json`을 컷오버 입력으로 넘긴다 | JSON 입력과 아래 네 가지 검사 |
| B. Executor가 직접 호출 | Executor가 Job을 만들고 `read_and_judge`를 부른다 | 이미지에 모듈 추가, 대상 `target-dev`의 jobs create/get · pods get/list · pods/log get Role. S3 Secret은 운영자가 미리 만든다 |

A의 결과 파일에는 서명이 없어, T12는 운영자가 넘긴 값을 믿는다. 더 확인하려면 T12가 `job_uid`로 Job을 읽어 대조한다. 그 읽기 권한이 B의 일부다.

`attachment_verdict.py`가 한 줄로 출력하는 JSON (`bravo.t8.attachment-copy-result/v1`):

```json
{
  "schema": "bravo.t8.attachment-copy-result/v1",
  "run_id": "demo-2026-11-07-r1",
  "plan_id": "Plan_2026-11-07",
  "epoch": 3,
  "verdict": "SUCCEEDED",
  "reason_code": "receipt_passed",
  "image_digest": "sha256:…",
  "job_uid": "…",
  "pod_uid": "…",
  "started_at": "2026-11-07T01:00:00Z",
  "finished_at": "2026-11-07T01:00:09Z",
  "job_failure_reason": null,
  "container_exit_code": 0,
  "receipt": {
    "passed": true,
    "source_count": 3,
    "destination_count": 3,
    "database_referenced_count": 3,
    "operator_approved_extra_count": 0,
    "copied": 3,
    "reused": 0,
    "bytes_verified": 1030,
    "key_content_inventory_sha256": "…",
    "content_sha256": ["…", "…", "…"]
  }
}
```

컷오버 전에 T12가 볼 것:

1. `schema`가 위 값과 같다. 다르면 시작하지 않는다.
2. `plan_id`와 `epoch`가 이번 컷오버 요청(`POST /v1/cutovers`)의 값과 같다. 다른 실행의 결과를 재사용하지 않는다. `execution_id`는 요청 뒤에 생기므로 복사 결과에 넣을 수 없다.
3. `verdict`가 `SUCCEEDED`다. `FAILED`와 `UNKNOWN`은 모두 시작하지 않는다.
4. `started_at`이 업로드를 멈춘 시각 이후다. 정지 전에 시작한 복사는 그 뒤의 업로드를 놓칠 수 있다.

쓰기 차단 뒤의 데이터 반영 검사는 그대로 둔다. 그때 `SELECT count(DISTINCT object_key) FROM public.attachments`와 `receipt.database_referenced_count`를 비교하면 복사와 쓰기 차단 사이의 업로드를 잡을 수 있다.

## 시험과 재지 못한 것

임시 k3s에서 렌더한 Job을 실제로 돌린 결과는 [증거](../evidence/t8-attachment-job-2026-11-06/README.md)에 있다. 아래 네 가지는 재지 못했다.

- **실제 AWS:** IAM · KMS 복호화 · `GetFederationToken` · 세션 정책 평가 · `ExpectedBucketOwner` 거부. 대역은 MinIO다.
- **실제 경로:** RKE2와 Canal의 NetworkPolicy, Tailscale egress, 대상 VM에서 S3로 나가는 HTTPS, 이 경로에서 걸리는 시간. 10분 제한은 추정값이다.
- **실제 이미지:** 게시 경로가 없고 amd64 빌드를 대상 VM에서 돌리지 않았다. 시험은 arm64 로컬 빌드다.
- **T12 연결:** 결과 JSON을 읽는 코드는 없다. 위 형식은 제안이다.

단위 시험과 변이 시험은 저장소만으로 돌릴 수 있다. 앞의 것은 CI에도 포함된다.

```sh
python3 -m unittest discover -s scripts/tests -p 'test_attachment_[jv]*.py'
python3 scripts/tests/attachment_job_mutation_check.py "$PRIVATE_DIR/mutations.json"
```

임시 k3s 실행은 수동이고, 만드는 자원은 모두 `t8-job-`로 시작하며 끝나면 지운다.

```sh
uv run --no-project --python 3.12 --with boto3==1.43.123 \
  python scripts/tests/attachment_job_k3s.py --out "$PRIVATE_DIR/k3s-result.json"
```
