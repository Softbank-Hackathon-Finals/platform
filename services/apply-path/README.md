# T31 승인 반영 경로: diff → 이미지 → 목적지 배포

설계 7.1의 6단계("사람 승인 → 반영")에서 **승인된 것과 배포되는 것이 같은 바이트인지**를 코드가 보장하는 부품이다(FR-7 승인 결속). 모델은 이 단계에 손대지 못한다. 상태 머신 · 승인 · 접수는 T10이, 리허설 검증은 T15가, 게이트 재대조는 T19가 맡고, T31은 그 사이를 digest로 묶는다.

**상태: 시험 41개 통과. 로컬 kind(Kubernetes v1.36.1)에서 `KubectlKube` 실제 경로 실측([기록](../../docs/evidence/t31-apply-entry-2026-11-06/README.md)). RKE2 목적지와 T10 · T19 연결은 미실측.**

| 파일 | 역할 |
|---|---|
| `binding.py` | T10 접수 영수증(binding) ↔ 승인된 제안서 ↔ 대상 환경 ↔ 시각 대조. diff는 T15와 **같은 함수**(`file_diff`)로 다시 그려 해시 |
| `deploy.py` | digest 고정 패치(`repository@sha256:…`, 태그 금지), rollout 대기, 실행 중 Pod의 digest 확인. 모든 클러스터 호출 오류를 patch 전/후로 구분해 `DeployMismatch`로. `FakeKube`(단위 시험 전용) · `KubectlKube`(실제) |
| `path.py` | `apply(application_id, …)` · `get_result(application_id, receipts_dir)`. 결속 → STARTED 기록 → 이미지 → 배포 → 게이트 재대조 요청. 결과는 스키마 검증, application_id당 기록 하나 |
| `cli.py` | `bravo-apply check / run / get-result / diff-hash` |
| `schemas/apply-result.schema.json` | 결과·기록 v1.1 (결과별 필드 조건 포함) |

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
- `failure_report()`: REFUSED · SAFE_HOLD · UNKNOWN의 `application_id` · `outcome` · `reason` · `change` · `cluster_mutation_attempted` · `previous_image` · `observed_digests` · `deadline_at` · `finished_at`.

### T10에 돌려주는 것

`ApplyOutcome.t10_application()`은 실제 클러스터(`cluster=kubectl`)의 APPLIED에서만 T10 `workflow.Application`이 계획 잠금 아래에서 대조하는 필드 그대로다(fake 결과는 `None`): `change{diff_hash, image_digest}` · `verification_id` · `approval_id` · `application_id` · `job_passed` · `deployed_digest`. T10은 `deployed_digest == change.image_digest`와 접수 영수증 일치를 다시 확인한다.

## 사용

```sh
bravo-apply check --claim claim.json --proposal proposal.json --target-env onprem-dev \
  --repository ghcr.io/<org>/demo-chat --sources ./snapshot      # 패치만 출력, 클러스터 접촉 없음
bravo-apply run   --application-id <claim의 application_id> --claim claim.json --proposal proposal.json --target-env onprem-dev \
  --repository ghcr.io/<org>/demo-chat --sources ./snapshot \
  --kube kubectl --kubeconfig /private/apply.kubeconfig --namespace target-dev --deployment demo-chat --receipts /durable/receipts
bravo-apply get-result --application-id <id> --receipts /durable/receipts [--t10]   # 클러스터 호출 없음
bravo-apply diff-hash --diff candidate.diff                        # T15가 넘긴 바이트의 해시
```

`--kube`는 필수다(`kubectl` 또는 `fake`, 기본값 없음). `fake`는 메모리 안의 가짜 클러스터라 아무것도 배포하지 않으며 출력에 `cluster: fake`와 `warning: FAKE_CLUSTER …`가 붙는다. `--kubeconfig`는 네임스페이스 한정 반영 identity(Deployment get · watch · patch, Pod get · list)만 담아야 한다. 클러스터 관리자 자격을 쓰지 않는다. 종료 코드: APPLIED 0 · REFUSED 2 · SAFE_HOLD 3 · UNKNOWN 4 · 기록 없음 5 · 기록 손상 6.

## 설계 결정 (ADR-0021)

- **이미지는 리허설이 검증한 digest를 승격**한다. 다시 빌드하면 digest가 달라져 승인 결속이 깨진다. T7의 digest 고정 발행 이미지를 그대로 쓴다.
- diff 해시는 T15 `credential_candidate.file_diff`와 같은 바이트(Git 호환 unified diff, 경로 정렬 연결)다. 패키지를 공유하지 않고 함수를 그대로 옮겨 시험으로 고정했다.
- 적용은 strategic merge patch로 컨테이너 이미지 하나만 바꾼다. GitOps · Helm은 본선 뒤 확장안.

## 남은 일 · 한계

- 실제 RKE2 목적지에서 `KubectlKube` 적용 · rollout · digest 확인 실측(T30의 demo-chat Deployment #210 병합 뒤). 로컬 kind 실측만 있다.
- T10 연결: 호출 규칙(1회 `apply`, 이후 `get_result`)은 합의했고 이 패키지가 두 진입점을 제공한다. T10 쪽 호출 코드와 영수증 디렉터리의 영속 배치(볼륨 · 실행 위치)는 남았다.
- UNKNOWN의 자동 재조정(live Deployment 읽기 대조)은 하지 않는다. 사람이 본다.
- 게이트 재대조는 요청 객체만 만든다. T19 호출 경로는 제어 평면이 연결한다.
- T24 기록: 결과 `steps`를 `source=executor`가 아닌 별도 source로 둘지 T24 계약에서 정한다.
