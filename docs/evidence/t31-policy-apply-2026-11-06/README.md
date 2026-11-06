# T31 버킷 정책 반영 분기 — 로컬 kind 클러스터 실측

2026-11-06 03:12–03:16 KST(2026-11-05 18:12–18:16Z)에 **이 노트북의 로컬 kind 클러스터**에서
실제 `bravo-apply run --kube kubectl` CLI의 버킷 정책 분기를 실행했다. **데모 목적지 RKE2가 아니다.**
RKE2 실측, T10 호출 연결, T19 재대조 연결, 실제 T15 리허설 영수증은 이 기록의 범위가 아니다.
측정을 마친 뒤 클러스터는 `kind delete cluster --name t31-policy`로 삭제했다(다른 클러스터는 없었고 건드리지 않았다).

## 환경

| 항목 | 값 |
|---|---|
| kind | v0.32.0 go1.26.3 darwin/arm64, 클러스터 이름 `t31-policy`, 단일 노드 |
| 노드 이미지 | `kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5` |
| Kubernetes | 서버 v1.36.1, kubectl 클라이언트 v1.36.1, containerd v2.3.1, Debian 13 arm64 |
| SeaweedFS | T9 `objects.py`의 고정 이미지 `docker.io/chrislusf/seaweedfs:4.52@sha256:a54ba35b44895a449f5cdd415999062201b17b9835ec0ffc19ea605b72ffa3d1` (Pod `imageID`도 같은 digest) |
| 보조 도구 | aws-cli 2.36.1(외부 관찰과 버킷 준비), curl 8.7.1(익명 GET 관찰) |
| 코드 | base `2222742072d9f3b6699c7a58b73b25f05e983073`(develop) + 이 브랜치의 미커밋 작업 트리. 실행 당시 파일 sha256은 아래 표. 실행 뒤 바뀐 것은 `policy.py`에서 S3 ARN 문자열을 f-string 대신 `string.Template`으로 만드는 한 줄과 docstring 표기뿐이다(공개 증빙 검사의 ARN 규칙 때문, 만들어지는 문자열은 같음). 그래서 커밋된 `policy.py` 해시는 아래 값과 다르다 |

| 파일 | sha256 |
|---|---|
| `services/apply-path/src/bravo_apply/policy.py` | `c68a916857a40c6685ff1e1b06e55ac163dbb9b0fea004883ff466ec83952f22` |
| `services/apply-path/src/bravo_apply/path.py` | `ec71d39b6db5553410768464b315bb6e7823b399270e3af25def823409261c15` |
| `services/apply-path/src/bravo_apply/binding.py` | `5aae0c63115fe2a290079fc0b196525615089e622eab258364d63367b45a651d` |
| `services/apply-path/src/bravo_apply/deploy.py` | `b833e9153d0b6b37d3f781d4cb51e8e1a99fb5df978cea0297a00eeb67dca273` |
| `services/apply-path/src/bravo_apply/cli.py` | `95e4dc28eb37edf141e774d96a442146f23acdd9ee228b2ffccb0312452cd645` |
| `services/apply-path/src/bravo_apply/schemas/apply-result.schema.json` | `d18b56930598bb00e17cc62f976a371b6d1c3fe58b2106c4275ffe80496c9e7a` |
| `infra/envs/target-workloads/object_exposure.py` (T9, 변경 없음) | `e60bd48a7aa08779b421ea62a6176006465be8cd826bc90aef38fef1adb697c2` — 기록의 `policy.renderer_sha256`과 같음 |
| `infra/envs/target-workloads/objects.py` (T9, 변경 없음) | `ff17cfde79668cb953e4d76de60c4f985c4201397dcbc4aef8de5e4220e939fd` |

## 구성

- 네임스페이스 `target-dev`에 T9 `objects.py --node-name <kind 노드>` 출력의 PV · PVC · Service `seaweedfs` ·
  Deployment `target-seaweedfs`를 그대로 적용했다(StorageClass `bravo-target-local`은 no-provisioner로 만들고 노드에
  local 경로를 uid 1000으로 준비). `s3.json`은 Secret `target-seaweedfs-auth`를 `objects.py`대로 읽기 전용 마운트한다.
- **적용하지 않은 것:** `objects.py`의 NetworkPolicy 두 개. 호스트의 CLI가 프로브할 수 있도록 시험 전용 NodePort
  Service(`seaweedfs-t31-nodeport`, 30833 → 호스트 127.0.0.1:18333)를 더했다. 실제 목적지에서 T31 실행 위치가
  8333에 닿는 경로는 별도로 정해야 한다(README 남은 일).
- 시작 상태: T9 `object_exposure.py render --state open-demo --rehearsal-bucket bravo-rehearsal-objects
  --acknowledge-anonymous-read`로 만든 `s3.json`(identity: bootstrap · client · anonymous · rehearsal-object-admin),
  T9 `plan`의 Secret 교체 명령 형태로 적용. 자격 증명은 실행마다 무작위로 만들어 저장소 밖 0600 파일에만 두었고,
  측정 뒤 삭제했다. bootstrap identity로 버킷 `demo-chat-attachments`와 객체 `t31/probe.txt`(sha256 `42e6cfab…`)를 만들었다.
  익명 GET 200을 확인했다.
- 같은 네임스페이스에 앱 대역 Deployment `demo-chat`(이미지 `registry.k8s.io/pause@sha256:ee6521f2…`)을 두고,
  binding의 `image_digest`를 이 digest로 했다. 정책 분기가 앱 이미지를 바꾸지 않는지 전후로 읽었다.
- identity(모두 `bravo-apply-access` 네임스페이스의 ServiceAccount, 2시간 토큰만 담은 kubeconfig):

  | identity | target-dev Role | 쓰임 |
  |---|---|---|
  | `apply-object-policy` | `secrets` get · update (`resourceNames: [target-seaweedfs-auth]`), `deployments` get · watch · patch (`resourceNames: [target-seaweedfs]`) — README에 문서화한 규칙 그대로 | (a)–(d) |
  | `apply-object-noupdate` | 위에서 secrets `update`만 뺌 | (e1) |
  | `apply-deployer` | `infra/envs/rehearsal-access/access.py target_apply()`와 같은 이미지 경로 규칙(`deployments` demo-chat get · watch · patch, `pods` get · list). Secret 권한 없음 | (e2) |

  `apply-object-policy`의 `kubectl auth can-i` 결과는 [rbac-can-i.txt](rbac-can-i.txt): 그 Secret get · update와 그
  Deployment get · watch · patch만 yes, secrets list · 다른 Secret · 삭제 · demo-chat Deployment · pods · exec는 no.
- 입력: `bravo_apply.binding`으로 만든 claim(`change_kind=bucket_policy`, `policy_digest`, `diff_hash`, `image_digest`=
  현재 앱 digest, `target_environment_id=kind-local`, 만료 +2시간)과 제안서(편집 하나, `src/target-policy.json`),
  원본 스냅샷. 원본 정책은 공개 읽기(Allow `*` GetObject), 승인 정책은 T15 `rke2_rehearsal_flow.py`의 restricted
  정책과 같은 모양(Deny `*` GetObject + `aws:PrincipalType=Anonymous`, 리허설 버킷 `bravo-rehearsal-objects` 대상,
  `sort_keys`)이다. 이 승인 정책은 이 실측용으로 만든 것이고 실제 T11 · T15 리허설 산출물이 아니다.
- `kubectl` 앞에 호출 기록 shim을 두어 경우별 CLI의 kubectl 호출을 셌다([kubectl-calls.log](kubectl-calls.log),
  kubeconfig 경로는 `<kc>`). Secret 본문은 stdin으로만 전달되어 로그에 없다.
- 외부 관찰([states.log](states.log), 관리자 kubeconfig는 읽기에만): 익명 `curl` 상태 코드, client 자격의 aws CLI
  GET, Secret의 `s3.json` sha256 · resourceVersion, SeaweedFS Deployment generation · 이미지 · Pod, 앱 이미지.
- open-demo로 되돌리기((b) 뒤, (e) 전)는 관리자 kubeconfig로 T9 `plan` 단계(Secret 교체 → rollout restart)를 그대로
  실행했다. T31이 한 일이 아니다.

실행 명령(경우마다 `--application-id`, 입력 디렉터리, `--kubeconfig`만 다름):

```sh
uv run --project services/apply-path bravo-apply run --kube kubectl --kubeconfig <apply-object-policy.kubeconfig> \
  --application-id pol-a-0001 --claim in/pol-a-0001/claim.json --proposal in/pol-a-0001/proposal.json \
  --sources in/pol-a-0001/snap --target-env kind-local --namespace target-dev --receipts <receipts> --timeout 180 \
  --object-endpoint http://127.0.0.1:18333 --probe-key t31/probe.txt --bucket demo-chat-attachments \
  --policy-bucket bravo-rehearsal-objects
uv run --project services/apply-path bravo-apply get-result --application-id pol-a-0001 --receipts <receipts> [--t10]
```

## 결과

`s3.json` 해시: open-demo `44cda0b9…`, closed `96a86e47…`. closed 해시는 같은 자격으로 T9 CLI
`object_exposure.py render --state closed --rehearsal-bucket bravo-rehearsal-objects`를 따로 실행한 출력의
`s3_json_sha256`과 같았다(바이트 동일). "kubectl 호출"은 shim이 센 CLI의 호출 수다.

| 경우 | application_id | identity | 결과 · 이유 · 종료 코드 | 클러스터 변화(외부 관찰) | kubectl 호출 |
|---|---|---|---|---|---|
| (a) 승인된 닫기 정책 | `pol-a-0001` | apply-object-policy | **APPLIED** · APPLIED_AS_APPROVED · 0. `policy.anonymous_get=DENIED`, `authenticated_get=ALLOWED`, `deployed_digest=null`, `gate_recheck.tests=[IAM-01.1]`. 약 25초 | Secret `s3.json` 44cda0b9→96a86e47, rv 1410→1660. SeaweedFS generation 1→2(새 Pod), 이미지 불변. 익명 curl **200→403**, client GET 전후 성공(객체 해시 동일). 앱 이미지 불변 | 5 (get secret · get deployment · replace · patch 템플릿 주석 · rollout status) |
| (b) 같은 id 재호출 | `pol-a-0001` | 같음 | APPLIED, (a) 출력과 **바이트 동일** · 0. stderr `existing record returned unchanged` | 불변(rv 1660, generation 2) | **0** |
| (b2) `get-result` · `--t10` | `pol-a-0001` | — | (a) 출력과 바이트 동일 · 0. `--t10`은 `change{change_kind, policy_digest, …}` · `applied_policy_digest` · `deployed_digest=null` | 불변 | **0** |
| — open-demo로 되돌림(관리자, T9 plan) | | | | 익명 200, rv 1830, generation 3 | |
| (c) 승인 뒤 정책 내용 변경 | `pol-c-0002` | apply-object-policy | **REFUSED** · DIFF_HASH_MISMATCH · 2. `change=null`, `cluster_mutation_attempted=false` | **Secret 불변**(44cda0b9, rv 1830), 익명 200 유지 | **0** |
| (d1) 승인 정책이 여전히 익명 읽기 허용(binding은 일치) | `pol-d-0003` | apply-object-policy | **REFUSED** · POLICY_GRANTS_ANONYMOUS_READ · 2 (번역 단계, STARTED 전) | 불변 | **0** |
| (d2) 닫히지 않는 목적지: 앱 버킷에 공개 버킷 정책이 남아 있음 | `pol-d-0004` | apply-object-policy | **SAFE_HOLD** · ANONYMOUS_READ_STILL_ALLOWED · 3. `anonymous_get=ALLOWED` | `s3.json` 44cda0b9→96a86e47(closed), rv 1830→1954, generation 3→4. 익명 curl **여전히 200**. 자동 롤백 없음(Secret은 closed 그대로) | 5 |
| (d2') 같은 id 재호출 | `pol-d-0004` | 같음 | 원래 SAFE_HOLD, 바이트 동일 · 3 | 불변 | **0** |
| — 사람이 남은 버킷 정책 삭제(bootstrap `delete-bucket-policy`) | | | | 익명 **403**, client GET 성공 | |
| — open-demo로 되돌림(관리자, T9 plan) | | | | 익명 200, rv 2080, generation 5 | |
| (e1) Secret update 권한 없는 identity | `pol-e-0005` | apply-object-noupdate | **SAFE_HOLD** · SECRET_REPLACE_FAILED · 3. step에 `Forbidden … cannot update resource "secrets"` 한 줄 | **불변**(서버가 거부: rv 2080, 44cda0b9, generation 5, 익명 200) | 3 (get · get · replace Forbidden) |
| (e2) Secret 권한 없는 identity(이미지 경로 Role) | `pol-e-0006` | apply-deployer | **REFUSED** · CLUSTER_READ_FAILED · 2. `cannot get resource "secrets"` | 불변 | 1 |
| (e2') 같은 id 재호출 | `pol-e-0006` | 같음 | 원래 REFUSED 그대로 · 2 | 불변 | **0** |

각 경우의 최종 기록은 [records/](records/)에 있다(`application_id`당 한 파일). 공개본은 `scripts/public_evidence.py sanitize`로 `target.endpoint`(호스트 쪽 NodePort 주소) 하나를 `RESOURCE_NAME_001`로 가린 사본이고, 원본은 저장소 밖에 보존했다([redaction-manifest](../privacy/redaction-manifest.json)). 가린 사본도 `get_result`로 스키마 검증을 통과한다. 기록 · stdout · stderr · 호출 로그에
자격 증명 값 6개가 하나도 들어 있지 않음을 대조 스크립트로 확인했다. 앱 Deployment `demo-chat`의 이미지는 모든
경우의 전후에 `sha256:ee6521f2…`로 같았다. `apply-object-policy`는 그 Deployment를 읽을 권한조차 없다.

## 해석과 한계

- (d)를 두 가지로 나눴다. **승인 정책 자체가 익명 읽기를 허용하면(d1) T31은 번역 단계에서 거부한다.** T31은 T9의
  `open-demo`를 렌더하지 않는다. T9가 그 상태에 명시적 확인과 데모 실행 ID를 요구하는 이유와 같고, T11도 `*`에 대한
  Allow를 거부하므로 이런 binding은 상류 결함일 때만 생긴다. SAFE_HOLD 경로는 **실제로 닫히지 않는 목적지(d2)**로
  확인했다: `s3.json`은 승인대로 closed가 되었지만 앱 버킷에 남은 공개 버킷 정책 때문에 익명 GET이 계속 200이었고,
  관찰 판정이 이를 잡아 SAFE_HOLD로 멈췄다.
- (d2)에서 확인된 사실: SeaweedFS 4.52는 `s3.json`에 anonymous identity가 없어도 버킷 정책의 `Allow *
  s3:GetObject`로 익명 읽기를 허용한다. 따라서 `s3.json` closed만으로 닫힘이 보장되지 않는다. 앱 버킷에 버킷 정책이
  옮겨지지 않았는지는 T8 · T9 운영 확인 사항이며, T31은 반영 뒤 관찰로만 판정한다.
- (e1)은 서버가 update를 거부해 실제로는 바뀌지 않았지만, T31은 쓰기 요청을 보낸 뒤의 실패를 이미지 경로의
  `PATCH_FAILED`와 같이 보수적으로 SAFE_HOLD로 둔다. 읽기 단계에서 막히면(e2) REFUSED다.
- 같은 `application_id`의 재호출은 결과 종류와 무관하게(APPLIED · REFUSED · SAFE_HOLD) kubectl을 한 번도 부르지
  않았다. SIGKILL에 의한 UNKNOWN은 정책 분기에서는 단위 시험(`restart_workload` 직전 중단)으로만 확인했고 kind에서는
  다시 하지 않았다(이미지 경로는 [이전 실측](../t31-apply-entry-2026-11-06/README.md)).
- 실행 원본(입력 · stdout · stderr · 상태 스냅샷)은 저장소 밖 비공개 경로에 보관했다. 토큰을 담은 kubeconfig와
  자격 증명 파일은 삭제했으며 커밋하지 않았다.
- 미검증: RKE2 목적지, `objects.py` NetworkPolicy 아래에서의 T31 → 8333 경로, 실제 T11 제안 · T15 리허설 영수증과의
  연결, T10이 `change_kind` · `policy_digest`를 보내는 연결(T10 미구현), 여러 노드, 영속 PV 위 실제 첨부 데이터.
