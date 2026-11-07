# [ADR-0044] S2의 버킷 닫기는 에이전트 infra.plan에서 코드가 만들고, 같은 승인으로 T31이 적용한다

* **상태 (Status):** 제안됨(Proposed). 11/7 23:4x KST 결정(M3 담당 df, S2 담당 41 동의). 해피패스 단일 API 지시(11/7)를 따른다.
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T31 #33, T10 #19, T44(ADR-0032), T49(#712 PROPOSE, #716 사전 위임), #709(쓰는 주체 하나), ADR-0038

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** S1의 데모 결함은 목적지 첨부 버킷의 익명 읽기(IAM-01.1 FAIL)다. 설계상 S2에서 AI가 고치고, 적용 뒤 재대조(`apply-recheck:1`)가 PASS가 돼야 한다.
* **문제:** 지금 흐름에서는 이 수정이 끝까지 이어지지 않는다(11/7 23:3x 확인).
  * PROPOSE(#712)는 demo-chat 파일 수정(이미지 후보)만 낸다. 제안 스키마에 버킷 정책 종류가 없다.
  * workflow host의 REHEARSE·GATE는 이미지 적용만 받는다. platform-events에 `bucket_policy` 처리는 0건이다. `ApplicationEvidence`는 `binding.image_digest == deployed_digest`를 요구하고, `GateRecheck`는 요청이 정확히 이미지 형식일 때만 받는다.
  * T31 라이브러리에는 닫기 경로가 이미 있다: `apply_bucket_policy`, T44 계획 검사 `check_iac_plan`. 하지만 T10 계획 흐름과 이어져 있지 않다.
* **목표:**
  * 사람의 결정 하나(변경 승인 또는 사전 위임)가 이미지 변경과 버킷 닫기를 함께 덮는다.
  * 버킷에 쓰는 주체는 T31 하나다(#709).
  * AI는 계획만 하고 실행 권한이 없다.

## 2. 고려한 옵션들 (Considered Options)

### 1. 제안 스키마에 `bucket_policy` 종류를 더한다

**Cons**
* AI가 정책 문서를 직접 쓰게 된다. 스키마·검증기·T15 리허설 경로가 한꺼번에 넓어진다.
* ADR-0032가 이미 정한 "에이전트는 infra.plan으로만 목적지 구성을 계획한다"와 겹친다.

### 2. T44 원하는 상태 PR(closed) → `desired-exposure`

**Cons**
* 승인이 Git 병합이 되어 T10 계획·위임과 두 갈래가 된다. T10 상태 흐름(REHEARSE → GATE)에 연결돼 있지 않다.

### 3. 에이전트의 수락된 `infra.plan`에서 결정적으로 만든다 (선택)

에이전트는 이미 `infra_plan` 도구를 가진다(`AgentPlanner`). 그 결과는 시도 기록 `infra_plans`에 실리고, 노출 확대는 도구 계층에서 거부된다(`IAC_WIDENING_REFUSED`). 코드는 수락된 계획 하나를 버킷 닫기로 바꿔 T31 정책 경로로 적용한다.

## 3. 결정 사항 (Decision Outcome)

* **3을 선택한다.**
* **PROPOSE (41):**
  * 제안 Job에 커밋된 원하는 상태 `--iac-state infra/iac/environments/<목적지 환경>.json`을 붙인다.
  * 결과의 `infra_plans`(`iac-plan.v1` 원문 그대로)를 적용 입력 기록(`apply_input_key(diff_hash)`)의 새 필드 `infra_plans`에 저장한다.
  * 기록은 지금처럼 한 번만 쓴다.
* **승인 binding (df):** `ChangeIntake.current`가 binding에 `iac_plan_digest`를 더한다. 그 diff_hash의 적용 입력 기록에서 아래 조건을 모두 만족하는 계획이 **정확히 하나**면 그 `plan_digest`다(T31 `check_closing_plan`). 없으면 키를 넣지 않아 기존 binding과 같다. 서로 다른 계획이 둘이면 `iac_plan_ambiguous`로 거부한다.
  * `kind`가 `iac-plan.v1`이다.
  * `accepted`가 true이고 `refusals`가 비어 있다.
  * `plan_digest`가 자기 일치한다.
  * `effects`에서 `target-object-exposure`의 익명 접근이 before ≠ [] → after = []다.
  * 환경: 계획의 IaC 환경 이름(`target-dev`)은 T10 환경 ID(`env-onprem`)와 다르다. 그래서 binding 단계에서는 대조하지 않는다. 버킷을 닫는 적용기가 자기가 설정된 목적지 IaC 환경과 대조한다.
  * 변경 승인과 사전 위임(#716, change bounds 비어 있음)은 이 binding을 그대로 승인하므로, 사람의 결정에 자동으로 묶인다.
* **REHEARSE (df): T31 적용 하나가 이미지와 닫기를 함께 한다.**
  * binding에 `iac_plan_digest`가 있으면, workflow host의 적용 입력(`ApplyInput`)에 그 계획과 설정된 목적지 노출 대상을 넣는다. 계획은 적용 입력 기록의 `infra_plans`에서 digest로 고르고, 대상은 workflow host 설정의 IaC 환경·버킷·SeaweedFS를 쓴다.
  * T31 `apply`는 이미지 배포가 승인대로 끝난 뒤 `apply_planned_exposure`로 버킷을 닫는다. 진입점은 `check_closing_plan`으로 설정된 IaC 환경과 대조하고, `apply_bucket_policy`를 부르며, #709의 주인 표시를 남긴다. 결과 ID는 `iac-<plan_digest 앞 16자>`(`planned_application_id`)로 결정적이다.
  * 그 적용 하나의 결과(ApplyOutcome)는 닫기가 끝난 뒤에야 APPLIED이고, `finished_at`도 닫기 뒤다. 닫기가 실패하면 그 적용은 SAFE_HOLD다(이미지는 되돌리지 않는다, 기존 T31 규칙).
  * 그래서 T10의 `Application` 기록 형식, REHEARSE 판독기, 재대조 요청(이미지 형식, `GATE_RECHECK_TESTS`에 IAM-01.1 포함)은 바뀌지 않는다.
  * 별개의 결과 두 개로 두지 않는 이유가 있다. 적용 뒤 GATE는 목적지 run을 한 번 묶으면 다시 묶지 않는다. 두 적용 사이(버킷이 아직 열림)에 시작한 run이 묶이면 BLOCK에 고정된다.
* **GATE (df):**
  * 적용 뒤 맥락은 계약대로 `apply-recheck:1`(SEC·IAM·ENC)이다.
  * 적용 뒤 목적지 run은 그 적용의 `finished_at`(닫기 뒤) 이후에 시작해야 한다. 기존 검사 그대로다.
  * 측정은 플랫폼 수집 생산자(#765, ENC는 #758 `pooler-tls`)가 한다. 서명은 ADR-0038 플랫폼 서명이 한다.
* **Git 원하는 상태:** `target-dev.json`의 `open-demo`는 데모 시작 상태로 남는다.
  * 런타임에서 닫는 주체는 이 경로 하나다. Argo는 그 Secret을 관리하지 않고(`bravo.dev/unmanaged`), `desired-exposure`는 열지 않는다.
  * 그래서 두 번째 런타임 writer가 생기지 않는다. Git을 `closed`로 맞추는 PR은 컷오버 뒤 후속으로 둔다.

## 4. 결과

* 승인 하나로 S2의 IAM-01.1 FAIL → 닫기 → 적용 뒤 PASS가 이어진다. 사람 개입은 위임 기록뿐이다.
* 바뀌는 곳:
  * 41: initial proposal 연결, `infra_plans` 저장
  * df: `ChangeIntake.current` binding, T31 계획 닫기 진입점, 적용 입력과 T31 `apply`의 이미지 뒤 닫기
* 바뀌지 않는 곳: 제안 스키마, T15 리허설, T19 검증기, `workflow.Application` 형식, REHEARSE·GATE 판독기
* 실환경 반영 전제(이 결정의 코드와 따로):
  * apply 신원 권한: `access.py target-apply --exposure`(#823). Secret `target-seaweedfs-auth` get/update와 Deployment `target-seaweedfs` get/watch/patch를 이름으로 한정한다.
  * workflow host 설정 `exposure`와 그 `repo_root`에 플랫폼 Git checkout을 마운트한다(`read_planned`가 base_revision을 읽는다).
  * **T31 확인 요청의 도달:** 닫은 뒤 T31은 SeaweedFS에 익명 GET 거부와 앱 자격 GET 허용을 직접 확인한다. AWS의 workflow host는 목적지 API(tailnet)에만 닿고, 목적지 클러스터 안의 SeaweedFS 서비스에는 닿지 않는다. 확인 요청이 닿지 않으면 그 적용은 SAFE_HOLD다(실패 쪽으로 닫힘). 그래서 SeaweedFS를 apply 경로 태그에만 grant한 tailnet-ingress로 선언해야 한다(T44·T30). 서명 GET은 경로가 바뀌면 SigV4가 깨지므로 API 서버 service proxy로는 대신할 수 없다.
* 남은 확인: 에이전트가 IAM-01.1 FAIL을 보고 infra.plan을 실제로 부르는지는 S2 실측으로 확인한다. 부르지 않으면 `iac_plan_digest`가 `null`이라 닫기 없이 GATE가 IAM-01.1에서 BLOCK한다(실패 쪽으로 닫힘).
