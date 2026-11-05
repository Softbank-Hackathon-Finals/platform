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
* 미검증: 실제 모델이 `inventory.describe`를 호출한 실측, T15 코드 수정 템플릿과의 통합, `rehearsal.exec` 계약.

## 5. 실측 (Evidence)

로컬 `services/agent-worker` 시험 323개 통과(새 시험: 소스 경로 정책 9, inventory.describe 3, policy.evaluate 6, 파괴 시도 기록 5). 실제 모델 호출 실측은 아직 없다.

## 6. 보완: T42 rehearsal.exec 도구의 T11 쪽 (2026-11-06)

* **배경:** 2절에서 미룬 `rehearsal.exec`를 T42가 동작별 도구 세 개와 [호출 계약 v1](../contracts/T42-rehearsal-exec-v1-proposal.md)로 정했고, T11 담당이 11/6 02:41에 형식을 확인했다(apply 인자는 `plan.propose`처럼 검사하되 제출 1회와 따로 센다, 남은 모델 실행 예산이 제출할 여유를 남기지 못하면 T11이 exec를 닫는다, strict 모드 수용은 실제 모델로 나중에 확인한다).
* **결정:**
  * 주입한 exec 채널이 켜져 있을 때만 `rehearsal.exec.apply`·`inspect`·`probe`를 노출한다. 아니면 `TOOL_NOT_PROVIDED`이며, 예약 이름 `rehearsal.exec`·`run`·`result`는 채널과 관계없이 그대로다.
  * 인자는 계약 스키마와 필드 사이 규칙을 코드로 검사하고, exec 호출은 인자 어디에든 파괴 패턴이 있으면 거부한다(`plan.propose`는 실패한 제안서에서만 범주를 기록한다).
  * T11은 채널 실패와 계약 밖 응답에 계약 5절의 응답을 직접 만들고, 결과 모름 뒤의 exec 호출은 보내지 않는다. 남은 모델 호출·시간이 예약분(`ExecReserve`, 기본 2턴·45초)보다 적으면 exec를 닫는다.
  * 모델로 가는 출력은 T15 검사 뒤에도 T11 `safe_text`를 한 번 더 거친다.
* **감사 사유 추가:** `EXEC_CLOSED`(T11이 채널로 보내지 않은 exec 호출: `exec_reason`이 `BUDGET_EXHAUSTED`·`UNAVAILABLE`·`EFFECT_UNKNOWN`). 보낸 호출은 `POLICY_ACCEPTED`에 `request_id`·`result_code`·`exec_reason`을 더한다. 인자 값은 기록하지 않는다.
* **미검증:** 실제 채널(T38 broker → T15) 연결, T38이 exec 사용 여부를 싣는 제품 입력 위치, 실제 모델의 strict 스키마 수용과 도구 사용, T15 `exec_events`와 `request_id` 대조. 시험은 채널 테스트 더블, OpenAI SDK 모의 전송(API 키·구독 스트림), Claude SDK 훅·MCP 핸들러 직접 호출까지다.
