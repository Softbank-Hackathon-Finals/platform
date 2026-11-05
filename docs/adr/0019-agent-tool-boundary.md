# [ADR-0019] 에이전트 도구 계층 경계: 소스 경로 정책 · 읽기 전용 인벤토리 · 정책 사전 린트 · 파괴 시도 기록

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-05
* **관련 (Related):** T11 #20, T15 #24, T16 #25, T24 #28, T38 #150. 설계 8.3(도구 화이트리스트 · 가드레일), 8.4(리허설 데모 장면), [ADR-0007](0007-model-provider-and-s2-ci.md), [ADR-0018](0018-dependency-grading-rules.md)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** T11 워커는 `repo.read` · `plan.propose` 두 도구만 열어 두었다. 리허설 러너(T15) README는 T11 담당자에게 세 가지 확인을 요청했다. (1) `safe_path`가 이름에 `secret`이 든 파일을 거부해 demo-chat의 `app/db_secret.py`(Secrets Manager 직접 호출 지점, 설계 8.4의 P0 코드 수정 대상)를 snapshot에 넣을 수 없다. (2) `rehearsal.exec`가 없어 버킷 삭제 시도가 일반 `TOOL_NOT_ALLOWED`로만 남아 설계 8.4의 데모 장면("삭제 시도가 도구 계층에서 막힌 기록")을 식별할 수 없다. (3) 설계 8.3의 `inventory.describe` · `policy.evaluate`가 없어 에이전트가 T16 등급과 정책 린트를 쓸 수 없다.
* **목표:** 모델에 주는 권한을 늘리지 않으면서 위 세 가지를 코드로 고정한다. 감사 기록에는 코드가 정한 열거값만 남긴다.

## 2. 고려한 옵션들 (Considered Options)

### 소스 경로
1. **이름 규칙을 코드 파일에서만 완화 (채택)**: `secret`이 든 이름은 소스 확장자(`.py` · `.ts` · `.go` 등)일 때만 허용. 내용은 기존 `safe_text`가 계속 검사한다.
2. 내용을 가려서(masking) 전달: 모델이 가린 내용을 기준으로 교체본을 쓰면 `before_sha256`(원본 해시)와 어긋나고, 가림 표식이 교체본에 섞일 수 있다. 기각.
3. 파일별 예외 목록: 앱마다 목록을 관리해야 하고 우회 경로가 된다. 기각.

### 인벤토리 전달
1. **`InventorySummary` 계약으로 부분집합만 전달 (채택)**: 항목 id · role · grade · grade_by · rationale · agent_needed · risk · 코드 참조의 파일 · 행 · 종류 · target만. snippet, 배포 속성, 환경변수 출처 · 값은 제외. `source_sha`가 실행 revision과 다르면 붙이지 못한다.
2. T16 JSON 전체를 프롬프트에 포함: 32 KiB 상한과 비밀 값 경계가 흐려진다. 기각.

### 파괴 시도 기록
1. **도구 계층이 인자를 범주로만 분류해 기록 (채택)**: `bucket_delete` · `namespace_delete` · `terraform_destroy` · `db_drop` · `filesystem_rm`. 원문 인자는 기록하지 않는다. 미제공 화이트리스트 도구는 `TOOL_NOT_PROVIDED`.
2. `rehearsal.exec`를 지금 구현: 네임스페이스 한정 서비스 계정 · 송신 차단(T3 · T38) 실측이 먼저다. 이번 결정 범위 밖.

## 3. 결정 (Decision)

* `safe_path`: `credential` · `private_key` 이름, `secrets/` · `credentials/` 디렉터리, 키 · 상태 파일 확장자, 테스트 파일은 계속 거부. `secret`이 든 이름은 소스 확장자만 허용.
* 도구: `policy.evaluate`(항상), `inventory.describe`(제어 경로가 `InventorySummary`를 붙인 실행만). 둘 다 부작용 없음. `inventory.describe`의 등급은 규칙 결과이며 모델이 바꾸지 못한다(ADR-0018).
* 감사 사유 열거값 추가: `TOOL_NOT_PROVIDED`, `TOOL_NOT_ATTACHED`, `DESTRUCTIVE_ATTEMPT_BLOCKED`(+`category`). `policy.evaluate` 결과 사유: `LINT_PASSED` · `WILDCARD_OR_COMPLEMENT_GRANT` · `AMBIGUOUS_POLICY_JSON` · `MISSING_EFFECT` · `DESTRUCTIVE_COMMAND` · `UNSAFE_TEXT` 등.
* T24 감사 로그와 T21 콘솔은 위 열거값을 그대로 쓴다. 데모 8.4 장면은 `reason=DESTRUCTIVE_ATTEMPT_BLOCKED, category=bucket_delete` 이벤트로 식별한다.

## 4. 결과 (Consequences)

* 좋은 점: T15가 `app/db_secret.py`를 snapshot에 넣을 수 있다. 콘솔이 "안 만든 도구"와 "막힌 시도"를 구분한다. 모델이 T16 등급과 린트를 읽어 제안 품질을 올리되 판정 권한은 없다.
* 나쁜 점: 이름 규칙 완화만큼 내용 검사(`safe_text`)에 의존한다. `safe_text`는 알려진 패턴만 본다. 파괴 범주 정규식도 알려진 명령만 본다. 둘 다 T15 독립 검증과 T3 격리의 대체가 아니다.
* 연결(11/6): `bravo-agent analyze --inventory`와 `bravo-subscription run --inventory`가 T16 문서를 읽어 `InventorySummary`를 붙인다. `source_sha`가 다르거나 형식 · 크기가 맞지 않으면 다른 입력 오류처럼 `NEEDS_HUMAN`(종료 코드 2)이고, 붙인 실행의 결과에만 `inventory_digest`가 남는다. 현재 `bravo-inventory scan` 출력(demo-chat 시험 입력, `--source-sha` 지정)을 `Tools.dispatch`와 OpenAI 함수 루프(가짜 전송)로 통과시켰다.
* T38 제품 경로 제안(미구현, T38 · T10 합의 필요): `ProductInput`에 선택 필드 `inventory`(T16 v1 문서 원본, 기본 `null`)를 두고 Admission digest가 이를 포함한다. `validate_product_input`은 `InventorySummary.from_document(inventory).source_sha == bundle.source_sha`를 검사하고, `invoke_product_worker`는 `inventory.json`(0400)을 쓰고 `SimpleNamespace(..., inventory=path)`로 넘긴다. `proposal_result`는 결과의 `inventory_digest`가 입력 문서의 `digest`와 같은지 확인한다. `ProductRequest`와 T11 입력(`FailureBundle`)은 바꾸지 않는다.
* 미검증: 실제 모델이 `inventory.describe`를 호출한 실측, T15 코드 수정 템플릿과의 통합, `rehearsal.exec` 계약.

## 5. 실측 (Evidence)

로컬 `services/agent-worker` 시험 323개 통과(새 시험: 소스 경로 정책 9, inventory.describe 3, policy.evaluate 6, 파괴 시도 기록 5). 실제 모델 호출 실측은 아직 없다.

## 6. 보완: 재시작 뒤 시도 이어 가기 (2026-11-06)

* **배경:** coordinator의 시도 수·상태·제안·영수증이 프로세스 메모리에만 있어, 러너가 재시작하면 2~3번째 시도를 이어 갈 수 없었다. 새 프로세스가 시도 수를 0부터 세면 3회 상한이 무너진다.
* **결정:** T11은 `Coordinator.export_state()`·`Coordinator.restore()`와 상태 전이마다 부르는 `checkpoint`를 제공한다. 저장은 T10/T15가 한다. 모델 호출 전에 `ANALYZING`을 먼저 저장(write-ahead)하고, 그 저장이 실패하면 모델을 부르지 않는다. `ANALYZING`으로 저장된 상태는 결과를 모르므로 `NEEDS_HUMAN`으로 복원하고 그 시도를 쓴 것으로 센다. 복원은 `state_digest`와 내용 규칙(시도 기록 수, 3회 상한, 상태와 기록·영수증의 일치, snapshot·inventory digest)을 모두 확인하고, 하나라도 어긋나면 이어 가지 않는다. 필드 목록은 [T11 README](../../services/agent-worker/README.md#재시작과-상태-보존).
* **감사 사유 추가:** `RESTART_OUTCOME_UNKNOWN`, `STATE_CHECKPOINT_FAILED`(`tool=coordinator`, `outcome=STOPPED`).
* **한계:** `state_digest`는 서명이 아니다. 위조 방지와 단일 실행(같은 `revision`에서 두 프로세스가 동시에 시작하는 것)은 T10 저장소의 접근 제어와 `revision` 비교 후 쓰기에 달려 있다. T10·T15 저장 연결과 실제 재시작 운영은 미검증이다.
* **실측:** 로컬 시험 `tests/test_coordinator_state.py`(왕복, 변조 거부, ANALYZING→NEEDS_HUMAN, 재시작을 넘는 3회 상한, OpenAI 모의 전송으로 실패→재시도→통과, 별도 프로세스 재개, `bravo-agent analyze --state-output`). 실제 모델·T10 저장소 연결 실측은 없다.
