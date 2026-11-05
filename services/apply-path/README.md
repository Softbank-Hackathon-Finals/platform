# T31 승인 반영 경로: diff → 이미지 → 목적지 배포

설계 7.1의 6단계("사람 승인 → 반영")에서 **승인된 것과 배포되는 것이 같은 바이트인지**를 코드가 보장하는 부품이다(FR-7 승인 결속). 모델은 이 단계에 손대지 못한다. 상태 머신 · 승인 · 접수는 T10이, 리허설 검증은 T15가, 게이트 재대조는 T19가 맡고, T31은 그 사이를 digest로 묶는다.

**상태: 로컬 구현 · 시험 23개 통과. 실제 클러스터 적용(`KubectlKube`)과 T10 · T19 연결은 미실측.**

| 파일 | 역할 |
|---|---|
| `binding.py` | T10 접수 영수증(binding) ↔ 승인된 제안서 ↔ 대상 환경 ↔ 시각 대조. diff는 T15와 **같은 함수**(`file_diff`)로 다시 그려 해시 |
| `deploy.py` | digest 고정 패치(`repository@sha256:…`, 태그 금지), rollout 대기, 실행 중 Pod의 digest 확인. `FakeKube`(시험) · `KubectlKube`(실제) |
| `path.py` | 전체 경로 한 함수: 멱등 → 결속 → 이미지 → 배포 → 게이트 재대조 요청. 결과는 스키마 검증, 영수증은 application_id당 1회 |
| `cli.py` | `bravo-apply check / run / diff-hash` |
| `schemas/apply-result.schema.json` | 결과 v1 |

## 경로

```
T10 claim_application 영수증 ─┐
승인된 T11 제안서(edits) ──────┼─ binding 검증 ─ 이미지 = 리허설 검증 digest(재빌드 없음) ─ 패치(digest 고정)
스냅샷 원본 또는 T15 diff 바이트 ┘      │                                                      │
                                      REFUSED(이유 코드)                       rollout + 실행 digest 확인
                                                                                   │            │
                                                                              SAFE_HOLD      APPLIED → T10 Application 보고
                                                                              (자동 롤백 없음)          + 게이트 재대조 요청(SEC-01.1 · IAM-01.1 · ENC-01.1)
```

### 결속 검증이 거부하는 것 (이슈 #33 "digest 다르면 거부 · 승인 diff 범위 안에서만")

| 코드 | 조건 |
|---|---|
| `DIFF_HASH_MISMATCH` | 제안서 + 원본으로 다시 그린 unified diff의 sha256이 binding과 다름 |
| `BASE_DIGEST_MISMATCH` · `SOURCE_MISSING` | 제안서의 `before_sha256`가 실제 원본과 다름 / 원본 없음 |
| `DIFF_OUTSIDE_APPROVED_PATHS` | diff가 제안서 밖 파일을 건드림 |
| `APPROVAL_EXPIRED` · `TARGET_ENVIRONMENT_MISMATCH` | binding 만료 / 대상 환경 불일치 |
| `PROPOSAL_RUN_MISMATCH` · `CLAIM_INCOMPLETE` · `BINDING_DIGEST_FORMAT` | 식별자 · 형식 |
| `ALREADY_APPLIED` | 같은 `application_id` 영수증이 이미 있음(클러스터 호출 전 거부) |

배포 뒤 실행 중 Pod의 digest가 승인 digest와 다르면 `SAFE_HOLD`다. 이전 digest로 되돌리는 것은 사람이 본 뒤 한다(설계 8.2).

### T10에 돌려주는 것

`ApplyOutcome.t10_application()`은 T10 `workflow.Application`이 계획 잠금 아래에서 대조하는 필드 그대로다: `change{diff_hash, image_digest}` · `verification_id` · `approval_id` · `application_id` · `job_passed` · `deployed_digest`. T10은 `deployed_digest == change.image_digest`와 접수 영수증 일치를 다시 확인한다.

## 사용

```sh
bravo-apply check --claim claim.json --proposal proposal.json --target-env onprem-dev \
  --repository ghcr.io/<org>/demo-chat --sources ./snapshot      # 패치만 출력, 클러스터 접촉 없음
bravo-apply run   --claim claim.json --proposal proposal.json --target-env onprem-dev \
  --repository ghcr.io/<org>/demo-chat --sources ./snapshot \
  --kube kubectl --kubeconfig /private/apply.kubeconfig --namespace target-dev --deployment demo-chat --receipts ./receipts
bravo-apply diff-hash --diff candidate.diff                        # T15가 넘긴 바이트의 해시
```

`--kubeconfig`는 네임스페이스 한정 반영 identity(Deployment get · patch, Pod list)만 담아야 한다. 클러스터 관리자 자격을 쓰지 않는다.

## 설계 결정 (ADR-0021)

- **이미지는 리허설이 검증한 digest를 승격**한다. 다시 빌드하면 digest가 달라져 승인 결속이 깨진다. T7의 digest 고정 발행 이미지를 그대로 쓴다.
- diff 해시는 T15 `credential_candidate.file_diff`와 같은 바이트(Git 호환 unified diff, 경로 정렬 연결)다. 패키지를 공유하지 않고 함수를 그대로 옮겨 시험으로 고정했다.
- 적용은 strategic merge patch로 컨테이너 이미지 하나만 바꾼다. GitOps · Helm은 본선 뒤 확장안.

## 남은 일 · 한계

- 실제 RKE2 목적지에서 `KubectlKube` 적용 · rollout · digest 확인 실측(T30의 demo-chat Deployment #210 병합 뒤).
- T10이 `claim_application` 뒤 이 경로를 어떻게 부르는지(동기 호출 vs 접수 큐)와 결과 보고 API. 현재는 CLI · 라이브러리.
- 게이트 재대조는 요청 객체만 만든다. T19 호출 경로는 제어 평면이 연결한다.
- T24 기록: 결과 `steps`를 `source=executor`가 아닌 별도 source로 둘지 T24 계약에서 정한다.
