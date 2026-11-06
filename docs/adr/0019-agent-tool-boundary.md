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

## 7. 보완: 시도별 도구 기록 추출과 T38 응답 연결 제안 (2026-11-06)

열린 PR #372(재시작 상태)와 #373(T42 `rehearsal.exec`)이 각각 §6을 쓰고 있어 번호 충돌을 피하려고 §7로 둔다.

* **배경:** T15(배규태)가 T38 제품 응답에 실행에 결속된 도구 기록을 넣어, 실제 모델로도 콘솔의 "버킷 삭제 시도 차단" 장면에 데이터가 있게 할 수 있는지 물었다. coordinator 결과에는 이미 시도별 `audit`과 `audit_event_count`·`audit_truncated`가 있지만, 추출 규칙은 T15 `bucket_proposer.attempt_tool_events`에만 있었고 T38은 `tool_events_status: "UNAVAILABLE"`로 고정한다.
* **결정:** T11이 정본 추출·검증 함수를 둔다(`bravo_agent.tool_events`). `attempt_tool_events(source, attempt)`는 `Coordinator`와 그 `result()` dict 모두에서 동작하고, 그 시도의 `independent_verifier` 외 이벤트를 원래 순서대로, 시도 기록이 하나이고 잘리지 않았으며 개수가 `audit_event_count`와 같을 때만 돌려준다. `validate_tool_events(events, run_id, attempt)`는 최대 256개, 이벤트마다 필수 필드·필드 이름·값 형식(str·int·bool만)과 run·attempt 결속을 확인한다. 같은 규칙의 JSON Schema를 `TOOL_EVENTS_SCHEMA`로 공개한다. 어느 하나라도 어긋나면 목록 전체를 내보내지 않는다(일부를 걸러 내지 않는다). #372의 `tool=coordinator` 중단 이벤트는 그 시도의 수에 들어가므로 병합 전후 모두 맞다.
* **T38 제안(미구현, T38 담당 류지환 님 동의 필요):** `proposal_result`가 `tool_events_projection(result, 1, run_id=bundle.run_id)`로 `tool_events_status: "COMPLETE"`+`tool_events` 또는 `"UNAVAILABLE"`(키 없음)을 내고, `validate_bound_product_response`가 `COMPLETE`일 때 `validate_tool_events`로 다시 확인한다. 필드 이름과 모양은 T15 `collect_tool_events`와 같다. 상세는 [T11 README](../../services/agent-worker/README.md#t38-응답-연결-제안-미구현-t38-담당-동의-필요).
* **한계:** 구독 경로(`chatgpt-plan`)의 첫 응답 진단 이벤트 `FIRST_RESPONSE_SHAPE`는 중첩 카운터와 `null`을 담아 계약 밖이다. 따라서 T38이 연결해도 구독 경로의 시도는 지금 모두 `UNAVAILABLE`이다. 진단 이벤트를 스칼라로 펴는 후속 작업이 있어야 실제 모델 장면에 데이터가 생긴다.
* **실측:** `tests/test_tool_events.py` — T15 함수 사본을 기준으로 OpenAI 모의 전송의 실제 coordinator 실행 2회(1회째 `rehearsal_exec` 버킷 삭제 차단·읽기·제안·FAIL, 2회째 제안·PASS)와 중단 시도에서 결과 일치, JSON 왕복 결과 dict, 잘림·누락·중복·다른 run 거부, #372 모양(중단 이벤트·재시작 시도) 모의와 #372 병합 뒤에만 도는 실제 경로 시험(checkpoint 실패 중단, ANALYZING 복원. #372 브랜치를 임시 병합해 통과 확인), 스키마와 검증기 일치(`jsonschema`), 256개 상한, 구독 경로의 `UNAVAILABLE`. 실제 모델 실측과 T38 연결은 없다.
