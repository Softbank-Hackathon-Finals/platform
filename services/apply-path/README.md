# T31 승인 반영 경로: diff → 이미지 또는 버킷 정책 → 목적지 반영

설계 7.1의 6단계("사람 승인 → 반영")에서 **승인된 것과 배포되는 것이 같은 바이트인지**를 코드가 보장하는 부품이다(FR-7 승인 결속). 모델은 이 단계에 손대지 못한다. 상태 머신 · 승인 · 접수는 T10이, 리허설 검증은 T15가, 게이트 재대조는 T19가 맡고, T31은 그 사이를 digest로 묶는다. 변경 종류는 두 가지다: 앱 이미지 변경(Deployment 이미지 digest 고정)과 버킷 정책 변경(SeaweedFS `s3.json` Secret 교체, [아래](#버킷-정책-변경-분기)).

**상태: 시험 91개 통과. 로컬 kind(Kubernetes v1.36.1)에서 `KubectlKube` 실제 경로 실측 — 이미지 분기([기록](../../docs/evidence/t31-apply-entry-2026-11-06/README.md)), 버킷 정책 분기([기록](../../docs/evidence/t31-policy-apply-2026-11-06/README.md)). RKE2 목적지와 T10 · T19 연결은 미실측. T10은 아직 `change_kind` · `policy_digest`를 보내지 않는다.**

| 파일 | 역할 |
|---|---|
| `binding.py` | T10 접수 영수증(binding) ↔ 승인된 제안서 ↔ 대상 환경 ↔ 시각 대조. diff는 T15와 **같은 함수**(`file_diff`)로 다시 그려 해시. `change_kind=bucket_policy`면 `policy_digest`도 대조 |
| `deploy.py` | digest 고정 패치(`repository@sha256:…`, 태그 금지), rollout 대기, 실행 중 Pod의 digest 확인. 모든 클러스터 호출 오류를 변경 전/후로 구분해 `DeployMismatch`로. `FakeKube`(단위 시험 전용) · `KubectlKube`(실제). 정책 분기용 `get_secret` · `replace_secret` · `restart_workload` |
| `policy.py` | 버킷 정책 분기: 승인 정책 → T9 노출 상태 번역, T9 렌더러로 `s3.json` closed 생성, Secret 교체 · 워크로드 재시작 · 익명/서명 GET 관찰 |
| `path.py` | `apply(application_id, …)` · `get_result(application_id, receipts_dir)`. 결속 → (정책 번역) → STARTED 기록 → 배포 또는 정책 반영 → 게이트 재대조 요청. 결과는 스키마 검증, application_id당 기록 하나 |
| `cli.py` | `bravo-apply check / run / get-result / diff-hash` |
| `schemas/apply-result.schema.json` | 결과·기록 v1.2 (결과별 · 변경 종류별 필드 조건 포함). v1.1 기록도 그대로 읽는다 |

## 경로

```
apply(application_id) ─ 기록 있음? ── 예 ─→ 그 기록 그대로 반환(클러스터 호출 없음)
                          │ 아니오
T10 claim_application 영수증 ─┐
승인된 T11 제안서(edits) ──────┼─ binding 검증 ─ STARTED 기록(UNKNOWN) ─ 이미지 = 리허설 검증 digest(재빌드 없음) ─ 패치(digest 고정)
스냅샷 원본 또는 T15 diff 바이트 ┘      │                │                                                   │
                                      REFUSED(이유 코드)  get 실패 → REFUSED                     rollout + 실행 digest 확인
                                                                                                   │            │
                                                                     (모든 결과가 기록을 교체) SAFE_HOLD      APPLIED → T10 Application 보고
                                                                                              (자동 롤백 없음)  + 게이트 재대조 요청(SEC-01.1 · IAM-01.1 · ENC-01.1)
```

### 결속 검증이 거부하는 것 (이슈 #33 "digest 다르면 거부 · 승인 diff 범위 안에서만")

| 코드 | 조건 |
|---|---|
| `DIFF_HASH_MISMATCH` | 제안서 + 원본으로 다시 그린 unified diff의 sha256이 binding과 다름 |
| `BASE_DIGEST_MISMATCH` · `SOURCE_MISSING` | 제안서의 `before_sha256`가 실제 원본과 다름 / 원본 없음 |
| `DIFF_OUTSIDE_APPROVED_PATHS` | diff가 제안서 밖 파일을 건드림 |
| `APPROVAL_EXPIRED` · `TARGET_ENVIRONMENT_MISMATCH` | binding 만료 / 대상 환경 불일치 |
| `PROPOSAL_RUN_MISMATCH` · `CLAIM_INCOMPLETE` · `BINDING_DIGEST_FORMAT` | 식별자 · 형식 |
| `APPLICATION_ID_MISMATCH` | `apply`에 넘긴 id와 claim의 `application_id`가 다름 |
| `CLUSTER_READ_FAILED` | patch 전 Deployment 읽기 실패(권한 거부·없음·컨테이너 없음·시간 초과). 클러스터는 바뀌지 않음 |
| `CHANGE_KIND_UNKNOWN` · `CHANGE_KIND_AMBIGUOUS` | binding의 `change_kind`가 모르는 값 / 이미지 변경인데 `policy_digest`가 있음 |
| `IMAGE_TARGET_MISSING` · `POLICY_TARGET_MISSING` | 변경 종류에 맞는 반영 대상(`--deployment` · `--repository` / `--object-endpoint` 등)이 설정되지 않음 |

patch를 보낸 뒤의 실패는 모두 `SAFE_HOLD`다: `PATCH_FAILED`(서버가 받았을 수 있음) · `ROLLOUT_STATUS_FAILED` · `ROLLOUT_NOT_COMPLETE` · `RUNNING_DIGEST_UNREADABLE` · `RUNNING_DIGEST_MISMATCH`. 이전 digest로 되돌리는 것은 사람이 본 뒤 한다(설계 8.2). 기록의 `previous_image`와 `observed_digests`가 그 판단 근거다.

### 1회 반영과 재시작 (ADR-0021 7절)

T10은 첫 영속 접수에서만 `apply`를 부르고, 응답 손실·재시작·기존 접수에서는 `get_result`만 부른다([계약](../../docs/contracts/T10-change-approval.md#반영-접수와-재시작)).

| 기록 `outcome` | 뜻 | T10 |
|---|---|---|
| `APPLIED` | 실행 Pod 전부 승인 digest | `t10_application()`으로 대조 |
| `REFUSED` | 클러스터를 바꾸지 않음(`cluster_mutation_attempted=false`). 결속 거부 또는 patch 전 읽기 실패 | 보류. 고친 변경은 새 접수(새 id) |
| `SAFE_HOLD` | patch를 보냈고 승인 digest 실행을 확인하지 못함 | 보류. 사람이 확인 |
| `UNKNOWN` | STARTED 기록만 있음 — 진행 중이거나 중단됨(`deadline_at` 뒤면 중단) | 보류. 재배포 금지, 사람이 Deployment 확인 |
| 기록 없음(`None`) | `apply`가 시작되지 않음 | 첫 접수가 아니면 보류 |

- 같은 id의 두 번째 `apply`는 결과 종류와 무관하게 **원래 기록을 그대로** 돌려주고 클러스터를 부르지 않는다.
- STARTED 기록은 첫 클러스터 호출 전에 임시 파일 fsync → `link`로 만든다(이미 있으면 실패, 반쯤 쓴 기록 없음). 최종 결과는 같은 실행이 `os.replace`로 교체한다. patch 뒤 프로세스가 죽으면 `UNKNOWN`으로 남고 다시 patch하지 않는다.
- 손상된 기록은 `RecordUnreadable`이며 덮어쓰지 않는다.
- `failure_report()`: REFUSED · SAFE_HOLD · UNKNOWN의 `application_id` · `outcome` · `reason` · `change` · `cluster_mutation_attempted` · `previous_image` · `observed_digests` · `policy` · `deadline_at` · `finished_at`.
- 위 규칙은 두 분기에 똑같이 적용된다. 정책 분기의 STARTED 기록도 Secret 읽기 전에 만들고, `deadline_at`에 프로브 대기(2 × 105초)를 더한다.

### T10에 돌려주는 것

`ApplyOutcome.t10_application()`은 실제 클러스터(`cluster=kubectl`)의 APPLIED에서만 T10 `workflow.Application`이 계획 잠금 아래에서 대조하는 필드 그대로다(fake 결과는 `None`): `change{diff_hash, image_digest}` · `verification_id` · `approval_id` · `application_id` · `job_passed` · `deployed_digest`. T10은 `deployed_digest == change.image_digest`와 접수 영수증 일치를 다시 확인한다.

버킷 정책 변경의 APPLIED는 이미지를 배포하지 않았으므로 `deployed_digest=null`이고, 대신 `applied_policy_digest`를 준다: `change{diff_hash, image_digest, change_kind: "bucket_policy", policy_digest}` · `applied_policy_digest` · 나머지 동일. **현재 T10 `Application`/`Change`는 이 형식을 받지 못한다**(아래 "T10이 더해야 할 필드"). 그 전까지 정책 분기 결과는 T10 계획을 진행시키지 않는다.

### T10이 더해야 할 필드 (T10 담당 범위, 이 PR은 T10 코드를 바꾸지 않음)

11/5 T10 담당과 합의한 방향: 버킷 정책 변경은 현재 앱 `image_digest`를 그대로 두고, 번역된 정책을 `policy_digest`로 따로 결속하며, 변경 종류로 앱 이미지 변경과 구분한다.

| 위치 | 필드 | 값 |
|---|---|---|
| T15 → T10 `CurrentChange.binding`(후보) · `intake_binding()` · 승인 `binding` · 접수 영수증 `binding` | `change_kind` | `"bucket_policy"`. 없거나 `"app_image"`면 지금과 같은 이미지 변경. 다른 값은 T31이 `CHANGE_KIND_UNKNOWN`으로 거부 |
| 같음 | `policy_digest` | `"sha256:" + sha256(승인 제안서 edits[0].replacement를 UTF-8로 인코딩한 바이트)`. 다시 직렬화하지 않은 원문 그대로(`bravo_apply.binding.policy_digest`). 이미지 변경에는 넣지 않는다(넣으면 `CHANGE_KIND_AMBIGUOUS`) |
| 같음 | `image_digest` | 정책 변경에서도 필수. 반영 시점의 현재 앱 이미지 digest(바꾸지 않음) |
| `workflow.Change` | `change_kind` · `policy_digest` (선택, 기본 없음) | `asdict(change)`가 T31 `change`와 같아지도록. 이미지 변경은 지금처럼 두 필드만 |
| `workflow.Application` | `applied_policy_digest: str \| None`, `deployed_digest: str \| None` | 정책 변경은 `applied_policy_digest == change.policy_digest`이고 `deployed_digest is None`을 대조. 이미지 변경은 지금 규칙(`deployed_digest == change.image_digest`) 유지 |
| `_satisfied(Application)` | 영수증 대조 | `claim.binding.change_kind` · `policy_digest`도 결과의 `change`와 대조 |

T15 쪽: 정책 변경의 `diff_hash`는 지금처럼 `src/target-policy.json` 한 파일의 unified diff 해시다. T31은 정책 변경에 원본 스냅샷(`--sources`)을 요구한다(diff 바이트만으로는 적용할 정책 내용과 diff가 같다는 보장이 없음, `POLICY_SOURCES_REQUIRED`).

## 버킷 정책 변경 분기

이슈 #33 "정책 적용도 승인 diff 범위 안에서만". binding의 `change_kind=bucket_policy`일 때만 탄다. 앱 이미지는 읽지도 바꾸지도 않는다.

```
binding 검증(diff 재계산 · policy_digest 대조 · 편집은 src/target-policy.json 하나)
 → 승인 정책을 T9 노출 상태로 번역(순수; closed만 가능)                         ─ 아니면 REFUSED
 → STARTED 기록
 → Secret target-seaweedfs-auth 읽기 · SeaweedFS Deployment 읽기               ─ 실패 REFUSED CLUSTER_READ_FAILED
 → T9 render("closed", 현재 s3.json의 자격 그대로) = 현재 − anonymous ?         ─ 아니면 REFUSED TARGET_AUTH_UNRECOGNIZED
 → Secret 교체(resourceVersion 유지, stdin) → 템플릿 주석 patch로 재시작 → rollout
 → 익명 GET(T9 probe) = 401/403 이고 앱(client) 자격 서명 GET = 200 ?           ─ 아니면 SAFE_HOLD(자동 롤백 없음)
 → APPLIED + IAM-01.1 재대조 요청
```

**번역 규칙(`translate_policy`).** T11 제안은 S3 버킷 정책 JSON이고(T15는 리허설 버킷에 `put-bucket-policy`로 검증), T9 렌더러는 노출 상태(`closed` · `open-demo`)를 받는다. 형식이 달라 작은 결정적 번역기를 둔다. 받는 것은 `Version: 2012-10-17`, 문장 1–8개, 각 문장 `Effect` · `Principal`(`"*"` 또는 `{"AWS": "*"}`) · `Action`(`s3:GetObject`만) · `Resource`(`arn:aws:s3:::${policy_bucket}/*`만) · 선택 `Sid` · 선택 `Condition`(`{"StringEquals": {"aws:PrincipalType": "Anonymous"}}`만)이다. 결과:

| 승인 정책 | 결과 |
|---|---|
| 모든 문장이 익명 호출자에 대한 Deny | `closed` → 반영 |
| Allow가 하나라도 있음 | REFUSED `POLICY_GRANTS_ANONYMOUS_READ`. T31은 `open-demo`를 렌더하지 않는다(T9가 명시 확인을 요구하는 데모 전용 상태) |
| 조건 없는 Deny `*`(서명된 앱 읽기까지 막음) | REFUSED `POLICY_DENIES_AUTHENTICATED_READ`. T9 identity로 표현할 수 없고 리허설도 실패로 판정하는 정책 |
| 그 밖(다른 버킷 · 다른 Action · 이름 있는 주체 · 다른 조건 · NotPrincipal 등) | REFUSED `POLICY_UNTRANSLATABLE` · JSON이 아니거나 키 중복이면 `POLICY_UNPARSEABLE` |

`policy_bucket`(`--policy-bucket`)은 승인 정책이 가리키는 버킷이다. T11은 리허설 버킷 이름으로 정책을 쓰므로 보통 T9 `--rehearsal-bucket` 값이고, 기본값은 `--bucket`(앱 버킷)이다. 버킷 이름의 대응이 번역에서 유일하게 글자 그대로가 아닌 부분이다.

**T9 재사용.** `infra/envs/target-workloads/object_exposure.py`의 `render`와 `probe`를 그대로 쓴다. 이 파일은 패키지가 아닌 스크립트라 저장소 경로에서 `importlib`로 읽는다(표준 라이브러리만 import). 읽은 파일의 sha256을 기록의 `policy.renderer_sha256`에 남긴다. 저장소 밖(wheel 설치 · 컨테이너)에서 실행하면 파일이 없어 `T9_RENDERER_UNAVAILABLE`로 거부한다 — 실행 이미지에 넣거나 T9 모듈을 패키지로 나누는 것은 T9 담당과 정할 일이다. 자격 증명은 새로 받지 않고 현재 `s3.json`에 있는 bootstrap · client · rehearsal-object-admin 자격을 T9 렌더러에 그대로 넘긴다. 렌더 결과가 "현재 identity에서 anonymous만 뺀 것"과 다르면(다른 identity, 다른 권한, 다른 버킷) 거부한다. 그래서 이 분기의 실제 효과는 anonymous identity 제거뿐이고, 결과 바이트는 `object_exposure.py render --state closed`와 같다(kind 실측에서 바이트 동일).

**관찰 판정.** 재시작 뒤 T9 `probe`(서명 없는 GET)가 `DENIED`(401/403)이고, 현재 `s3.json`의 client 자격(앱 identity)으로 서명한 GET(SigV4, 표준 라이브러리 구현)이 200이어야 APPLIED다. 통제 IAM-01.1의 질문과 같다. 저장소가 재시작 중이면(연결 오류 · 5xx) 최대 90초 동안 3초 간격으로 다시 묻는다. 결과의 `policy`에 `previous_s3_json_sha256` · `applied_s3_json_sha256` · `anonymous_get` · `authenticated_get`을 남긴다(자격 값은 기록 · 로그 어디에도 없음). 이 분기에서 `previous_image`는 SeaweedFS 워크로드 이미지(읽기만 하고 바꾸지 않음)이고 `observed_digests`는 비어 있다.

| 정책 분기 이유 코드 | 결과 | 뜻 |
|---|---|---|
| `POLICY_*` · `T9_RENDERER_UNAVAILABLE` | REFUSED | STARTED 전, 클러스터 호출 없음 |
| `CLUSTER_READ_FAILED` · `TARGET_AUTH_UNRECOGNIZED` | REFUSED | Secret/Deployment 읽기 실패 또는 T9가 렌더하지 않는 목적지. 바뀐 것 없음 |
| `SECRET_REPLACE_FAILED` · `RESTART_FAILED` · `ROLLOUT_STATUS_FAILED` · `ROLLOUT_NOT_COMPLETE` | SAFE_HOLD | 쓰기 요청을 보낸 뒤의 실패(서버가 거부했어도 보수적으로) |
| `ANONYMOUS_READ_STILL_ALLOWED` · `PROBE_UNKNOWN` · `AUTHENTICATED_READ_FAILED` · `PROBE_FAILED` | SAFE_HOLD | 바꾼 뒤 관찰이 승인 상태가 아님. 이전 `s3.json`으로 되돌리는 것은 사람이 본 뒤 |

kind 실측에서 앱 버킷에 공개 버킷 정책이 남아 있으면 `s3.json`을 closed로 바꿔도 익명 GET이 200이었다(SeaweedFS 4.52는 버킷 정책의 `Allow *`를 따른다). T31은 이것을 `ANONYMOUS_READ_STILL_ALLOWED` SAFE_HOLD로 멈췄다. `s3.json`만으로 닫힘이 보장되지 않으므로 관찰 판정을 생략하지 않는다.

### 필요한 RBAC (정책 분기 identity, 최소 권한)

이미지 분기 identity(`access.py target_apply()`: `deployments` demo-chat get · watch · patch, `pods` get · list)와 **분리한** ServiceAccount를 권한다. 이미지 identity가 객체 저장소 자격을 읽지 못하게 하기 위해서다. SeaweedFS가 있는 네임스페이스(`target-dev`)의 Role:

| apiGroups | resources | resourceNames | verbs | 쓰는 호출 |
|---|---|---|---|---|
| `""` | `secrets` | `target-seaweedfs-auth` | `get`, `update` | `kubectl get secret … -o json`, `kubectl replace -f -` |
| `apps` | `deployments` | `target-seaweedfs` | `get`, `watch`, `patch` | `kubectl get deployment`(이미지 기록), 템플릿 주석 patch, `kubectl rollout status` |

`list` · `create` · `delete`, 다른 Secret, pods, exec는 필요 없다(kind에서 `auth can-i` no 확인). `get secret`은 곧 객체 저장소 bootstrap · client · rehearsal 자격을 읽는 권한이다 — 현재 identity를 보존하고 앱 자격으로 서명 GET을 하려면 필요하다. `patch deployments`는 필드를 제한하지 못하므로 이론상 SeaweedFS 이미지도 바꿀 수 있다. T31 코드는 템플릿 주석만 patch한다. 이 Role · ServiceAccount의 실제 발급(`access.py`에 `target_object_policy()` 같은 렌더러 추가)은 인프라 담당 범위라 이 PR에 넣지 않았다. 네트워크: `objects.py`의 NetworkPolicy는 `bravo-object-client=true` 라벨 Pod만 8333에 들인다. T31 실행 위치에서 프로브가 닿을 경로가 필요하다(미정).

## 사용

```sh
bravo-apply check --claim claim.json --proposal proposal.json --target-env onprem-dev \
  --repository ghcr.io/<org>/demo-chat --sources ./snapshot      # 패치만 출력, 클러스터 접촉 없음
bravo-apply run   --application-id <claim의 application_id> --claim claim.json --proposal proposal.json --target-env onprem-dev \
  --repository ghcr.io/<org>/demo-chat --sources ./snapshot \
  --kube kubectl --kubeconfig /private/apply.kubeconfig --namespace target-dev --deployment demo-chat --receipts /durable/receipts
bravo-apply get-result --application-id <id> --receipts /durable/receipts [--t10]   # 클러스터 호출 없음
bravo-apply diff-hash --diff candidate.diff                        # T15가 넘긴 바이트의 해시
# 버킷 정책 변경(binding.change_kind=bucket_policy): 같은 run에 정책 대상만 준다
bravo-apply run --application-id <id> --claim claim.json --proposal proposal.json --target-env onprem-dev --sources ./snapshot \
  --kube kubectl --kubeconfig /private/object-policy.kubeconfig --namespace target-dev --receipts /durable/receipts \
  --object-endpoint http://seaweedfs.target-dev.svc.cluster.local:8333 --probe-key <존재하는 객체 키> \
  --bucket demo-chat-attachments --policy-bucket <리허설 버킷>
```

변경 종류는 CLI 플래그가 아니라 승인된 binding이 정한다. 이미지 대상(`--deployment` · `--repository`)과 정책 대상(`--object-endpoint` · `--probe-key`)을 함께 줘도 binding 종류의 대상만 쓴다. `check`는 정책 변경이면 패치 대신 `policy_state`를 출력한다.

`--kube`는 필수다(`kubectl` 또는 `fake`, 기본값 없음). `fake`는 메모리 안의 가짜 클러스터라 아무것도 배포하지 않으며 출력에 `cluster: fake`와 `warning: FAKE_CLUSTER …`가 붙는다. `--kubeconfig`는 네임스페이스 한정 반영 identity(Deployment get · watch · patch, Pod get · list)만 담아야 한다. 클러스터 관리자 자격을 쓰지 않는다. 종료 코드: APPLIED 0 · REFUSED 2 · SAFE_HOLD 3 · UNKNOWN 4 · 기록 없음 5 · 기록 손상 6.

## 설계 결정 (ADR-0021)

- **이미지는 리허설이 검증한 digest를 승격**한다. 다시 빌드하면 digest가 달라져 승인 결속이 깨진다. T7의 digest 고정 발행 이미지를 그대로 쓴다.
- diff 해시는 T15 `credential_candidate.file_diff`와 같은 바이트(Git 호환 unified diff, 경로 정렬 연결)다. 패키지를 공유하지 않고 함수를 그대로 옮겨 시험으로 고정했다.
- 적용은 strategic merge patch로 컨테이너 이미지 하나만 바꾼다. GitOps · Helm은 본선 뒤 확장안.
- 버킷 정책 변경은 앱 이미지를 그대로 두고 `policy_digest`로 따로 결속한다. 목적지 반영은 T9 렌더러가 만든 `s3.json` closed로 Secret 하나를 교체하는 것뿐이며, 판정은 반영 뒤 익명 · 앱 자격 GET 관찰이다(ADR-0021 8절).

## 남은 일 · 한계

- 실제 RKE2 목적지에서 `KubectlKube` 적용 · rollout · digest 확인 실측(T30의 demo-chat Deployment #210 병합 뒤). 로컬 kind 실측만 있다.
- T10 연결: 호출 규칙(1회 `apply`, 이후 `get_result`)은 합의했고 이 패키지가 두 진입점을 제공한다. T10 쪽 호출 코드와 영수증 디렉터리의 영속 배치(볼륨 · 실행 위치)는 남았다.
- UNKNOWN의 자동 재조정(live Deployment 읽기 대조)은 하지 않는다. 사람이 본다.
- 게이트 재대조는 요청 객체만 만든다. T19 호출 경로는 제어 평면이 연결한다.
- T24 기록: 결과 `steps`를 `source=executor`가 아닌 별도 source로 둘지 T24 계약에서 정한다.
- 정책 분기: T10의 `change_kind` · `policy_digest` 결속과 `Application` 대조(위 표), 분리된 정책 identity의 실제 발급, T31 실행 위치 → SeaweedFS 8333 네트워크 경로, T9 렌더러를 저장소 밖 실행 환경에 싣는 방법, RKE2 실측. 앱 버킷에 남은 버킷 정책은 T31이 지우지 않는다(관찰로 SAFE_HOLD).
