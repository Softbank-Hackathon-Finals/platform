# T31 반영 진입점(apply · get_result) — 로컬 kind 클러스터 실측

2026-11-06 02:00–02:03 KST(2026-11-05 17:00–17:03Z)에 **이 노트북의 로컬 kind 클러스터**에서
실제 `bravo-apply run --kube kubectl` CLI 경로를 실행했다. **데모 목적지 RKE2가 아니다.**
RKE2 실측, T10 호출 연결, T19 재대조 연결은 이 기록의 범위가 아니다.
측정을 마친 뒤 클러스터는 `kind delete cluster --name t31-apply`로 삭제했다.

## 환경

| 항목 | 값 |
|---|---|
| kind | v0.32.0 go1.26.3 darwin/arm64 |
| 노드 이미지 | `kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5` |
| Kubernetes | 서버 v1.36.1, kubectl 클라이언트 v1.36.1 |
| 런타임 | containerd 2.3.1, Debian 13, arm64, 단일 노드 |
| 코드 | base `5760c449f2eb0f755d7c504b1997f587ed7735bf`(develop) + 이 브랜치의 미커밋 작업 트리. 실행 뒤 바뀐 것은 `path.py`의 영수증 디렉터리 생성 권한(`mode=0o700`) 한 줄이다. (a)·(b)는 CLI 출력 키 정렬(`sort_keys`) 추가 전이라 (b)는 JSON 의미 비교, (b2)부터는 바이트 비교다 |

이미지는 공개 `registry.k8s.io/pause`의 실제 manifest list digest다(레지스트리 `Docker-Content-Digest` 응답).

| 이름 | digest | 비고 |
|---|---|---|
| A (`pause:3.9`) | `sha256:7031c1b283388d2c2e09b57badb803c05ebed362dc88d84b480cc47f72a21097` | 초기 배포 |
| B (`pause:3.10`) | `sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a` | 승인 digest |
| 없는 digest 1 | `sha256:4f17b7f86647fddc927340914b38e3a4764b368bd0c5be835c6c0d8630dd8a58` | `sha256("t31-no-such-image")` |
| 없는 digest 2 | `sha256:0ef5b526d733c054900eb002d2baafef18c7c29ea75a67b8260d236ec3ec7595` | `sha256("t31-no-such-image-2")` |

containerd는 Pod `imageID`를 `registry.k8s.io/pause@<manifest list digest>`로 보고했다. 따라서 index digest로
고정 배포해도 실행 digest 확인이 같은 값으로 맞았다(ADR-0021 4절의 index/manifest 우려가 이 환경에서는 생기지 않음).

## 구성

- 네임스페이스 `t31-target`에 Deployment `demo-chat`(replicas 2, 컨테이너 `app`, 이미지 `pause@A`).
- 반영 identity: `bravo-apply-access/apply-deployer` ServiceAccount. Role은
  `infra/envs/rehearsal-access/access.py target_apply()`와 같은 규칙이다 — `deployments`
  get·watch·patch(`resourceNames: [demo-chat]`), `pods` get·list. 2시간 토큰(`kubectl create token`)만 담은
  kubeconfig로 CLI를 실행했다. 클러스터 관리자 kubeconfig는 상태 확인(읽기)에만 썼다.
- 대조용 identity `apply-reader-only`: 같은 읽기 권한, patch 없음.
- 입력: `bravo_apply.binding.diff_hash(render_diff(...))`로 만든 claim(binding: diff 해시, 이미지 digest,
  `target_environment_id=kind-local`, 만료 +2시간)과 제안서, 원본 스냅샷. (c)의 불일치는 승인 뒤 제안서
  replacement를 바꿔서 만들었다.
- `kubectl` 앞에 호출 기록용 shim을 두어 CLI가 부른 kubectl 명령을 경우별로 셌다([kubectl-calls.log](kubectl-calls.log),
  kubeconfig 경로는 `<kc>`로 가림). (a)는 shim을 두기 전에 실행해 호출 기록이 없다.

실행 명령(경우마다 `--application-id`, 입력 디렉터리, `--timeout`, `--deployment`, `--kubeconfig`만 다름):

```sh
uv run --project services/apply-path bravo-apply run --kube kubectl --kubeconfig <apply.kubeconfig> \
  --application-id kind-a-0001 --claim in/kind-a-0001/claim.json --proposal in/kind-a-0001/proposal.json \
  --sources in/kind-a-0001/snap --target-env kind-local --repository registry.k8s.io/pause --container app \
  --namespace t31-target --deployment demo-chat --receipts <receipts> --timeout 120
uv run --project services/apply-path bravo-apply get-result --application-id kind-a-0001 --receipts <receipts> [--t10]
```

## 결과

generation·RS 리비전은 관리자 kubeconfig로 실행 전후에 읽었다. "kubectl 호출"은 shim이 센 CLI의 호출 수다.

| 경우 | application_id | 결과 · 이유 · 종료 코드 | 클러스터 변화 | kubectl 호출 |
|---|---|---|---|---|
| (a) 승인 digest B 반영 | `kind-a-0001` | APPLIED · APPLIED_AS_APPROVED · 0 | generation 1→2, RS 리비전 2 생성. 실행 Pod 2개 `imageID=…@B`, `observed_digests=[B, B]`, `previous_image=pause@A` | 기록 전 |
| (b) 같은 id 재호출 | `kind-a-0001` | APPLIED(첫 결과와 JSON 동일) · 0. stderr `existing record returned unchanged` | generation 2 유지, RS·Pod 불변, rollout history 리비전 1·2만 | **0** |
| (b2) 같은 id 재호출 · get-result | `kind-a-0001` | 재호출 출력과 `get-result` 출력이 **바이트 동일**. `--t10`은 T10 `Application` 필드 그대로 | 불변 | **0** |
| (c1) 승인 뒤 바뀐 제안서 | `kind-c-0002` | REFUSED · DIFF_HASH_MISMATCH · 2. `change=null`, `cluster_mutation_attempted=false` | 불변 | **0** |
| (c2) 같은 id 재호출 · get-result | `kind-c-0002` | 원래 REFUSED · DIFF_HASH_MISMATCH 그대로(바이트 동일). `ALREADY_APPLIED`가 아님. `--t10`은 `failure` 보고 | 불변 | **0** |
| (c3) 고친 변경을 새 id로 | `kind-c-0003` | APPLIED · 0 (digest A로) | generation 2→3, 실행 Pod 2개 `…@A` | 5 (get · patch · rollout status · get · get pods) |
| (d) 받을 수 없는 digest, `--timeout 20` | `kind-d-0004` | SAFE_HOLD · ROLLOUT_NOT_COMPLETE · 3. `observed_digests=[A, A]`(기존 Pod), `previous_image=pause@A` | generation 3→4, 새 Pod `ImagePullBackOff`, 기존 Pod 2개 유지. 자동 롤백 없음 | 5 |
| (d2) 같은 id 재호출 | `kind-d-0004` | 원래 SAFE_HOLD 그대로(바이트 동일) · 3 | generation 4 유지 | **0** |
| (e) patch 뒤 rollout 대기 중 `kill -9` | `kind-e-0005` | 프로세스 종료 137. 남은 기록: UNKNOWN · APPLY_STARTED_NOT_FINISHED, `cluster_mutation_attempted=null`, `deadline_at` 기록 | generation 4→5 (patch는 들어감) | 3 (get · patch · rollout status) |
| (e2) 중단된 id 재호출 · get-result | `kind-e-0005` | UNKNOWN 그대로 · 종료 코드 4. 다시 patch하지 않음 | generation 5 유지 | **0** |
| (f) 권한 밖 Deployment(`other-app`) | `kind-f-0006` | REFUSED · CLUSTER_READ_FAILED · 2. Forbidden 원문 한 줄을 step에 기록, `cluster_mutation_attempted=false` | 불변 | 1 (get, Forbidden) |
| (g) patch 권한 없는 identity | `kind-g-0007` | SAFE_HOLD · PATCH_FAILED · 3. `cluster_mutation_attempted=true`, `previous_image` 기록 | 불변(서버가 거부). T31은 patch 전송 뒤 실패를 보수적으로 SAFE_HOLD로 둔다 | 2 (get · patch, Forbidden) |
| (h) `run`에 `--kube` 생략 | `kind-h-0008` | argparse 오류 `the following arguments are required: --kube` · 2 | 영수증 디렉터리도 만들지 않음 | 0 |

`--kube fake`는 이 실측에 쓰지 않았다(단위 시험에서만 사용). fake 결과는 `cluster: fake`와
`warning: FAKE_CLUSTER: …` 표시를 달고, `t10_application()`은 `None`을 돌려 T10에 배포로 보고되지 않는다.

각 경우의 최종 기록은 [records/](records/)에 있다. 반영 기록은 `application_id`당 한 파일이고, 실행 중
남은 임시 파일은 없었다(`kill -9` 뒤에도 기록 파일 5개뿐).

## 해석과 한계

- 같은 `application_id`의 두 번째 호출은 결과 종류와 무관하게(APPLIED · REFUSED · SAFE_HOLD · UNKNOWN)
  기록을 그대로 돌려주고 kubectl을 한 번도 부르지 않았다. 같은 digest를 다시 patch하면 Deployment가
  바뀌지 않으므로 generation 불변만으로는 증거가 약하다. 그래서 shim의 호출 수 0을 주 근거로 쓴다.
- (e)는 실제 SIGKILL이다. STARTED 기록이 patch 전에 디스크에 있었기 때문에 재시작 뒤 재배포가 일어나지
  않았다. 이 상태의 판정(되돌릴지, 다시 승인할지)은 사람이 Deployment를 보고 한다. `deadline_at`이 지나도
  최종 기록이 없으면 그 실행은 끝났다고 본다.
- (d)·(e) 뒤 Deployment는 실패한 rollout 상태로 남겨 두었다(설계 8.2: 자동 롤백 없음). 클러스터는 측정 뒤
  삭제했다.
- 실행 원본(입력·stdout·stderr·상태 스냅샷)은 저장소 밖 비공개 경로에 보관했다. 토큰을 담은 kubeconfig는
  삭제했으며 커밋하지 않았다.
- 미검증: RKE2 목적지, 실제 demo-chat 이미지, 여러 노드, `kubectl rollout status`의 장시간 대기, T10이
  `apply`·`get_result`를 부르는 연결, 영수증 디렉터리의 영속 볼륨 배치.
