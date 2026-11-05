# [ADR-0021] 승인 반영은 리허설이 검증한 이미지 digest를 그대로 승격하고, diff는 T15와 같은 바이트로 다시 해시한다

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-05
* **관련 (Related):** T31 #33, T10 #19, T15 #24, T19 #22, T7 #12. 설계 7.1(6단계), FR-7, 8.2 SAFE_HOLD, [T10 변경 승인 계약](../contracts/T10-change-approval.md), [ADR-0011 비공개 이미지 전달](0011-private-image-delivery.md)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** T10은 사람 승인을 `binding{diff_hash, image_digest, target_environment_id, expires_at}`에 묶고 반영을 1회 접수한다. T15는 제안서를 unified diff 바이트로 만들어 그 해시와 리허설 이미지 digest를 결속한다. 접수 뒤 "실제로 그 바이트를 목적지에 올리는" 부분이 비어 있었다.
* **문제:** (1) 반영 시점에 이미지를 다시 빌드하면 digest가 달라져 승인과 배포가 같은 바이트라는 보장이 사라진다. (2) diff 해시를 다른 방식으로 계산하면 T15 · T10 · T31이 같은 변경을 두고 다른 값을 가진다. (3) 배포가 절반만 되거나 다른 digest가 돌면 자동 롤백이 또 다른 미승인 변경이 된다.

## 2. 고려한 옵션들 (Considered Options)

| 결정 | 선택지 | 판단 |
|---|---|---|
| 이미지 | A. 반영 시 GitHub Actions로 재빌드 / B. **리허설 검증 digest 승격 (채택)** / C. 플랫폼 VM 빌드 | A · C는 재현 가능 빌드가 아니면 digest가 바뀐다. B는 검증한 바이트 = 배포 바이트. |
| diff 해시 | A. 제안서 JSON 정규화 해시 / B. **T15 `file_diff`와 같은 unified diff 바이트 (채택)** | A는 T15 · T10 binding과 값이 다르다. B는 함수를 그대로 옮겨 시험으로 고정. |
| 적용 방식 | A. **strategic merge patch로 이미지만 교체 (채택)** / B. GitOps 커밋 / C. Helm | 데모 기간에 새 도구를 넣지 않는다. B · C는 확장안. |
| 실패 처리 | A. 자동 롤백 / B. **SAFE_HOLD + 사람 (채택)** | 롤백도 변경이다. 설계 8.2 원칙. |
| 멱등 | **application_id당 영수증 1회, 두 번째는 클러스터 호출 전 거부** | T10 접수 영수증의 1회성과 짝. |

## 3. 결정 (Decision)

위 표의 채택안. 결속 검증은 제안서 `before_sha256` ↔ 원본, 재계산 diff 해시 ↔ binding, diff가 건드린 경로 ↔ 제안서 경로, 대상 환경 · 만료를 모두 확인하고 하나라도 다르면 `REFUSED`다. 배포 뒤 실행 Pod의 digest가 승인 digest와 같을 때만 `APPLIED`이며, 그때 T10 `Application` 필드와 게이트 재대조 요청(SEC-01.1 · IAM-01.1 · ENC-01.1)을 낸다.

## 4. 결과 (Consequences)

* 좋은 점: 승인 · 리허설 · 배포가 하나의 digest 쌍으로 묶인다. T10이 대조하는 필드를 그대로 돌려주므로 연결 비용이 작다.
* 나쁜 점: 리허설 이미지가 없으면 반영할 수 없다(의도한 제약). 적용 identity의 네임스페이스 한정 권한은 T7 · T30이 만들어야 한다. Pod digest 확인은 `imageID` 기준이라 레지스트리가 index digest와 manifest digest를 다르게 보고하는 경우 운영 규칙이 필요하다.
* 미검증: 실제 RKE2 적용, T10 호출 경로, T19 재대조 연결.

## 5. 실측 (Evidence)

로컬 `services/apply-path` 시험 23개(결속 거부 10종, diff 포맷 · 정렬, 패치 digest 고정, rollout · drift SAFE_HOLD, 멱등, CLI). 클러스터 실측은 없다.

## 6. T7 저장소 접근 범위 정합화 (2026-11-05)

현재 병합된 `bravo_apply.path.apply_approved_change`는 승인 결속을 검사하고
`bravo_apply.deploy`를 통해 검증된 digest를 배포한다. Git 커밋·브랜치 생성이나
GitHub 인증 입력은 없다. [담당자의 digest·diff 입력 설명](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793858043899199)과
이 구현을 대조하고, 사용자의 직접 해결 위임에 따라 T7의 남은 접근 기준을
분석용 reader의 수신 환경 인증 조회로 맞춘다. 이는 담당자가 Git writer 폐기에
별도 동의했다고 기록하는 것이 아니다.

T31에 Git writer를 주입하지 않으며, 기존 writer 발급·시험 이력은 보존한다.
T7 reader는 신뢰된 수집 과정에서만 사용하고 T11 모델·생성 코드에 전달하지 않는다.
향후 GitOps 커밋을 채택하면 소비 함수·승인 범위·보호된 주입 경로와 별도 수신
증빙을 먼저 정의한다. 이번 정합화는 T31의 정책 적용·게이트 재대조 완료를 뜻하지
않고, 위 ADR의 미검증 범위와 다른 태스크의 체크 상태도 바꾸지 않는다.

## 7. 반영 진입점과 재시작 안전 (2026-11-06 개정)

위 2절의 멱등 행("두 번째는 클러스터 호출 전 거부")과 3절의 `apply_approved_change` 단일 진입점을
아래로 고친다. 앞 절의 기록은 바꾸지 않는다. 근거는 T10 담당과 합의한 호출 규칙(11/6 01:33 Slack,
[계약 "반영 접수와 재시작"](../contracts/T10-change-approval.md#반영-접수와-재시작)):
T10은 첫 영속 접수(`newly_created=True`)에서만 `apply`를 1회 부르고, 응답 손실·재시작·기존 접수에서는
`get_result(application_id)`만 부른다. 결과가 없거나 불명확하면 보류하고 재배포하지 않는다. 큐는 두지 않는다.

* **고친 결함:** (1) REFUSED도 같은 영수증 파일을 써서 이후 호출이 `REFUSED/ALREADY_APPLIED`와 0 digest를
  돌려줬다 — 원래 거부 이유를 다시 읽을 수 없었다. (2) patch 뒤 영수증 쓰기 전에 프로세스가 죽으면 재실행이
  다시 patch했다. (3) `kubectl` get·patch 오류와 rollout 대기 subprocess 시간 초과는 기록 없이 예외로 빠졌다.
  (4) CLI `run`의 기본값이 `--kube fake`라 플래그를 빠뜨리면 아무것도 배포하지 않고 APPLIED를 출력했다.
* **결정:**
  * `apply(application_id, claim, proposal, …)`: `application_id`당 기록 하나(`<receipts>/<id>.json`).
    기록이 있으면 결과 종류와 무관하게 그대로 돌려주고 클러스터를 부르지 않는다. 결속 거부는 기록 없이
    순수 검사 뒤 바로 최종 REFUSED로 만든다. 결속이 맞으면 **첫 클러스터 호출 전에** STARTED 기록
    (`outcome=UNKNOWN`, `reason=APPLY_STARTED_NOT_FINISHED`, `deadline_at`)을 원자적·배타적으로 만들고, 끝나면
    같은 실행만 최종 결과로 교체한다. 생성은 임시 파일 fsync 뒤 `link`(EEXIST면 선행 호출이 이김)라서
    `O_EXCL`과 같은 배타성에 반쯤 쓴 기록이 남지 않는다. 교체는 `os.replace`, 둘 다 디렉터리 fsync.
  * `get_result(application_id, receipts_dir)`: 순수 읽기. 기록이 없으면 `None`, 손상되면
    `RecordUnreadable`(덮어쓰지 않음). UNKNOWN이면 T10은 보류하고 사람이 Deployment를 본다.
    `deadline_at`이 지나도 최종 기록이 없으면 그 실행은 중단된 것이다. 재조정(live Deployment 대조)은
    자동으로 하지 않는다 — 단순함을 택했고, 자동 대조가 필요해지면 읽기 전용으로 별도 결정한다.
  * 클러스터 오류: patch 전송 전(get 실패·권한 거부·컨테이너 없음)은 REFUSED `CLUSTER_READ_FAILED`
    (`cluster_mutation_attempted=false`, 변경 없음 보장). patch 전송 뒤(`PATCH_FAILED`,
    `ROLLOUT_STATUS_FAILED`, `ROLLOUT_NOT_COMPLETE`, `RUNNING_DIGEST_UNREADABLE`, `RUNNING_DIGEST_MISMATCH`)는
    서버가 받아들였을 수 있으므로 SAFE_HOLD. rollout 대기 subprocess 제한은 kubectl `--timeout`+30초.
    종료 중(deletionTimestamp) Pod는 실행 digest 확인에서 뺀다.
  * 보고: `t10_application()`은 실제 클러스터의 APPLIED만 그대로 돌려준다(fake는 `None`).
    `failure_report()`는 REFUSED·SAFE_HOLD·UNKNOWN에 `application_id`·`outcome`·`reason`·`change`·
    `cluster_mutation_attempted`·`previous_image`·`observed_digests`·`deadline_at`·`finished_at`을 준다.
  * 결과 스키마 1.1: `outcome`에 UNKNOWN, 거부 전 `change=null`, `cluster`·`target`·
    `cluster_mutation_attempted`·`previous_image`·`observed_digests`·`started_at`·`deadline_at`·`finished_at`
    추가, 결과별 필드 조건을 스키마로 고정.
  * CLI: `run`은 `--kube kubectl|fake`와 `--application-id`를 반드시 받는다. fake 결과는
    `cluster: fake`와 경고를 출력한다. `get-result [--t10]`을 추가했다. 종료 코드 APPLIED 0 · REFUSED 2 ·
    SAFE_HOLD 3 · UNKNOWN 4 · 기록 없음 5 · 기록 손상 6.
  * `apply_approved_change`는 `apply(claim["application_id"], …)`의 별칭으로 남기되 `receipts_dir`가 필수다.
* **결과:** 거부된 `application_id`는 그 이유로 고정된다. 고친 변경은 T10이 새 접수(새 `application_id`)로
  다시 부른다. 영수증 디렉터리는 T31 실행 위치의 영속 저장소에 있어야 한다(배치는 미정).
* **실측:** 단위 시험 41개(`uv run pytest`). 로컬 kind v0.32.0 · Kubernetes v1.36.1에서 실제 CLI 경로로
  APPLIED, 재호출 kubectl 0회, REFUSED 재조회와 새 id 반영, 받을 수 없는 digest의 SAFE_HOLD, patch 뒤
  SIGKILL의 UNKNOWN·재patch 없음, 권한 거부 두 가지를 확인했다
  ([기록](../evidence/t31-apply-entry-2026-11-06/README.md)). **RKE2 목적지가 아니다.** RKE2 · T10 연결 ·
  T19 재대조는 계속 미검증이다.

## 8. 버킷 정책 변경 분기 (2026-11-06 개정)

이슈 #33의 남은 항목 "정책 적용도 승인 diff 범위 안에서만"을 위 결정에 더한다. 앞 절의 기록은 바꾸지 않는다.
근거: 사용자 결정과 11/5 15:05 T10 담당과 맞춘 방향 — 버킷 정책 변경은 현재 앱 `image_digest`를 그대로 두고,
번역된 정책을 `policy_digest`로 따로 결속하며, 변경 종류로 앱 이미지 변경과 구분한다.

* **상황:** T11 버킷 제안은 `src/target-policy.json` 한 파일을 S3 버킷 정책 JSON으로 고치고, T15는 그것을 리허설
  버킷에 `put-bucket-policy`로 검증한다. 목적지 앱 버킷의 익명 노출은 T9가 렌더하는 SeaweedFS `s3.json`
  identity(closed · open-demo)가 정한다. 반영 단계에서 두 형식을 잇는 부분과, 이미지 변경과 구분되는 결속이 없었다.
* **고려한 옵션:**

  | 결정 | 선택지 | 판단 |
  |---|---|---|
  | 결속 | A. 정책도 `image_digest` 자리에 / B. **`change_kind` + `policy_digest` 별도 (채택)** | A는 이미지 변경과 구분되지 않고 앱 digest 대조를 깨뜨린다 |
  | `policy_digest` | A. 정규화 JSON 해시 / B. **승인된 replacement 원문 UTF-8 바이트의 sha256 (채택)** | A는 T10 · T15가 같은 정규화를 다시 구현해야 한다. B는 diff와 같은 원문을 쓴다 |
  | 목적지 반영 | A. 앱 버킷에 `put-bucket-policy`(리허설과 같은 방식) / B. **T9 렌더러로 `s3.json` closed를 만들어 Secret 하나 교체 (채택)** | A는 T31에 객체 저장소 Admin 자격을 준다. B는 T9가 이미 운영 절차로 가진 경로이고 Kubernetes 권한 두 줄로 끝난다 |
  | 형식 차이 | **작은 결정적 번역기(채택)**: 익명 GetObject Deny만 → `closed`, 그 밖은 거부 | 승인 정책이 익명 읽기를 허용하면 T31은 `open-demo`를 렌더하지 않고 REFUSED |
  | 판정 | A. 렌더 결과로 판정 / B. **반영 뒤 익명 GET 거부 + 앱 자격 GET 성공 관찰 (채택)** | 설계 원칙 "동등성은 시험 결과로". kind에서 남은 버킷 정책이 `s3.json` closed를 무력화하는 경우를 실제로 잡았다 |
  | 실패 | **SAFE_HOLD, 자동 롤백 없음 (기존과 같음)** | 이전 `s3.json`(open-demo)으로 되돌리는 것도 변경이다 |

* **결정:** binding의 `change_kind`가 분기를 정한다(없으면 이미지 변경). `bucket_policy`면 (1) 원본 스냅샷으로
  diff를 다시 그려 `diff_hash`를 대조하고, (2) 편집이 `src/target-policy.json` 하나이며 그 replacement의
  `policy_digest`가 binding과 같은지 보고, (3) 번역 결과가 `closed`인지 확인한다. 여기까지는 STARTED 전 순수 검사다.
  그 뒤 STARTED 기록 → Secret · Deployment 읽기 → 현재 자격 그대로 T9 `render("closed")`(현재 identity에서
  anonymous만 빠진 결과가 아니면 거부) → Secret 교체(resourceVersion 유지) → 템플릿 주석 patch로 재시작 → rollout →
  익명 · 앱 자격 GET 관찰. 이미지는 읽기만 하고 바꾸지 않는다. 멱등 · STARTED · REFUSED/SAFE_HOLD/UNKNOWN ·
  `get_result` 규칙은 7절과 같다. 결과 스키마 1.2: `change`에 `change_kind` · `policy_digest`, `policy` 객체
  (`s3.json` 해시 · 프로브 결과 · 렌더러 해시), 정책용 `target`, `gate_recheck.policy_digest`. 1.1 기록은 그대로
  읽는다. 정책 APPLIED는 `deployed_digest=null`, T10용 `applied_policy_digest`, 재대조 요청은 IAM-01.1.
* **결과:** 정책 반영 identity는 `target-dev`의 `secrets`(`target-seaweedfs-auth`) get · update와
  `deployments`(`target-seaweedfs`) get · watch · patch만 필요하다. 이미지 identity와 분리하길 권한다(`get secret`이
  객체 저장소 자격 읽기를 뜻함). T9 렌더러는 저장소 경로에서 읽으므로 저장소 밖 실행 환경에 싣는 방법이 필요하다.
  T10은 `change_kind` · `policy_digest`를 binding에 싣고 `Application`에서 `applied_policy_digest`를 대조해야
  한다(필드 목록은 apply-path README). 그 전까지 정책 분기 결과는 T10 계획을 진행시키지 않는다.
* **실측:** 단위 시험 91개. 로컬 kind v0.32.0 · Kubernetes v1.36.1, T9와 같은 SeaweedFS 4.52 고정 이미지와
  `objects.py` 렌더 결과(NetworkPolicy 제외)에서 실제 CLI로 APPLIED(익명 200→403, 앱 자격 GET 유지, 결과 `s3.json`이
  T9 CLI closed 렌더와 바이트 동일), 재호출 kubectl 0회, 승인 뒤 정책 변경 REFUSED(Secret 불변), 익명 허용 정책
  REFUSED, 남은 공개 버킷 정책 때문에 닫히지 않은 목적지 SAFE_HOLD, update 권한 없는 identity SAFE_HOLD와 Secret
  권한 없는 identity REFUSED를 확인했다([기록](../evidence/t31-policy-apply-2026-11-06/README.md)). **RKE2 목적지가
  아니다.** RKE2, T10 결속 연결, 실제 T11 · T15 산출물, T31 실행 위치의 네트워크 경로는 미검증이다.
