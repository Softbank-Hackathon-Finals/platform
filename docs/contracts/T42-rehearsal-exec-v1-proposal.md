# [T42] rehearsal.exec 호출 계약 v1 제안

상태: **v1 형식 T11 확인됨([전동훈 11/6 02:41](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793900490783729?thread_ts=1793899865.617069&cid=C0CDEFEB83U)). 구현 없음.** 결정 근거는 [ADR-0024](../adr/0024-rehearsal-exec-actions.md)(동작·인자·실행 위치), [ADR-0025](../adr/0025-rehearsal-exec-spaces-and-order.md)(공간 분리·순서), [ADR-0026](../adr/0026-rehearsal-exec-records-and-failures.md)(기록·실패 처리)다. 기계 검사용 원본은 [`T42-rehearsal-exec-v1.schema.json`](T42-rehearsal-exec-v1.schema.json)이고, 이 문서는 스키마로 표현하지 못한 규칙을 적는다. 두 파일이 다르면 둘 다 고치기 전까지 구현하지 않는다.

생산·소비: 모델 → T11(도구 노출·인자 검사·파괴적 요청 차단·감사) → 실행 전용 로컬 채널 → T38 broker → 고정 사설 TLS T15 경로 → T15 실행부(실행·실행 전후 기록·출력 비밀값 검사·정리) → 서명된 시도 기록 → T10 → T24 감사 로그·T21 콘솔. 통로와 신원은 T38이 정한다(Actions 신원은 broker에만 둔다). 이 문서는 통로 위의 메시지 형식만 정한다.

## 1. 도구 이름

설계 8.3의 `rehearsal.exec` 하나를 동작별 도구 세 개로 나눈다. 동작이 도구 이름으로 정해지므로 인자에 동작 종류를 넣지 않는다.

| 동작 | 모델에 보이는 이름 (OpenAI function / Claude MCP) | T11 내부 이름 | 기록의 `action` |
|---|---|---|---|
| ① 후보 띄우기·고치기 | `rehearsal_exec_apply` / `mcp__bravo__rehearsal_exec_apply` | `rehearsal.exec.apply` | `apply` |
| ② 상태·로그 보기 | `rehearsal_exec_inspect` / `mcp__bravo__rehearsal_exec_inspect` | `rehearsal.exec.inspect` | `inspect` |
| ③ 공간 안 접속 확인 | `rehearsal_exec_probe` / `mcp__bravo__rehearsal_exec_probe` | `rehearsal.exec.probe` | `probe` |

- 세 도구는 T15가 그 실행에 exec를 켰고 로컬 채널이 열려 있을 때만 노출한다. 끈 작업 종류에서 호출되면 지금처럼 `TOOL_NOT_PROVIDED`로 거부한다.
- **기본값은 꺼짐이다.** 러너 설정·제품 실행 입력·T11 모두 켜는 값이 없거나 읽을 수 없으면 끈 것으로 본다. 해피패스(시연 흐름) 검증이 끝날 때까지는 모든 작업 종류에서 끄고, 검증 뒤에 작업 종류별로 켠다. 끈 상태의 리허설은 exec 도입 전과 같은 순서·결과여야 하며, T42 변경마다 이를 시험으로 확인한다.
- 예약 이름 `rehearsal.exec`·`rehearsal.run`·`rehearsal.result`는 계속 `TOOL_NOT_PROVIDED`다. OpenAI 경로도 같은 사유로 기록한다(T11 PR #344에서 수정 중).

## 2. 모델 인자

모델이 정하는 값만 받는다. namespace, 실행 ID, 시도 번호, 공간 UID, 이미지, 서비스 계정, 볼륨, 명령은 인자에 없다. OpenAI strict 모드는 모든 속성을 필수로 만들기 때문에, 해당 없는 값은 생략하지 않고 `null`로 보낸다. 두 경로 모두 같은 스키마를 쓴다.

| 도구 | 필드 | 타입·범위 | 규칙 |
|---|---|---|---|
| apply | 제안서 전체 | T11 `Proposal` 스키마 그대로(`services/agent-worker/src/bravo_agent/contracts.py`) | `plan.propose`와 같은 검사(실행 문맥 일치, 수정 허용 경로, `before_sha256`, 파괴적 문자열). 이 문서에 사본을 두지 않는다 |
| inspect | `target_kind` | `pod` · `deployment` · `events` · `logs` | |
| | `target_name` | `^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$` 또는 `null` | `events`만 `null` 허용(공간 A 전체 이벤트) |
| | `lines` | 1~200 또는 `null` | `logs`일 때만 정수, 나머지는 `null` |
| probe | `service` | `^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$` | T15가 `<service>.<공간 A namespace>.svc`로만 조립한다 |
| | `port` | 1~65535 | |
| | `method` | `TCP` · `HTTP_GET` · `HTTP_HEAD` | |
| | `path` | `/`로 시작, 256자 이하, 경로·질의 문자만 또는 `null` | `TCP`면 `null`, HTTP면 필수 |

- 이름은 Kubernetes 이름 규칙(DNS 레이블)이라 하이픈으로 시작하거나 끝날 수 없다. 하이픈으로 시작하는 값이 kubectl 옵션으로 읽히지 않게 하기 위해서다.
- 필드 사이 규칙(위 표의 오른쪽 열)은 모델에 보내는 스키마에 넣지 않는다. strict 모드가 조건식을 받지 않기 때문이다. T11과 T15가 스키마 파일의 `inspect_rules`·`probe_rules`로 둘 다 검사한다.
- apply는 매번 스냅샷 원본에 대한 제안서 전체다. 두 번째 apply는 첫 초안 위에 덧붙이지 않고, 원본에 새 초안을 반영한 상태로 바꾼다. 러너는 공간 B 채점과 같은 적용 코드와 템플릿으로 반영한다(ADR-0024 3-3).
- apply는 `plan.propose`와 검사만 같이 쓴다. `plan.propose`의 시도당 제출 1회 제한에는 세지 않으며, apply 횟수는 4절의 exec 한도로만 센다.
- 인자에 러너가 넣은 리허설 비밀값이 원문·base64·URL 인코딩으로 들어 있으면 T15는 실행하지 않고(`BLOCKED`, `VALIDATE`), 기록에서 인자 키를 뺀다.

## 3. 서버가 붙이는 결속 값

모델 인자와 분리된 봉투에 담긴다. 모델은 이 값을 보거나 바꿀 수 없다.

| 값 | 채우는 곳 | T15 확인 |
|---|---|---|
| `request_id` | T11. 모델 도구 호출마다 새로 만들고 다른 호출에 다시 쓰지 않는다 | 시도 안에서 중복 처리 기준 |
| `args_sha256` | T11. `arguments`의 `json.dumps(sort_keys=True, separators=(",", ":"))` UTF-8 SHA-256(T11 `digest`와 같은 방식) | T15가 다시 계산해 다르면 `BLOCKED` |
| `run_id`, `attempt` | T38 broker. 지금 처리 중인 제품 실행에서 붙인다. 로컬 채널 메시지(`local_request`)에는 없다 | 열린 exec 세션과 다르면 `FOREIGN_ATTEMPT` |
| 공간 A UID | T15 서버. 열린 exec 세션이 가진 값으로 묶고 요청에 싣지 않는다 | 세션의 공간이 지워졌거나 UID가 바뀌었으면 `BLOCKED` |

- **같은 요청:** 같은 `request_id`와 같은 `args_sha256`이면 다시 실행하지 않고 저장된 결과를 돌려준다. 첫 실행이 아직 진행 중이면 끝날 때까지 기다려 같은 결과를 준다. 같은 `request_id`에 다른 `args_sha256`이면 `BLOCKED`다.
- **결과 모름:** apply 요청이 실행부에 넘어간 뒤 결과를 모르면 다시 실행하지 않는다. 그 호출은 `UNKNOWN`·`EFFECT_UNKNOWN`이고, 그 시도의 남은 exec 호출은 `REJECTED`·`EFFECT_UNKNOWN`이다. 제출과 공간 B 채점은 진행한다(ADR-0026 3-3).
- 결속이 맞지 않는 요청(`FOREIGN_ATTEMPT`)은 다른 시도의 기록에 넣지 않는다. 응답만 돌려주고, T15 서버 로그에는 사유 코드만 남긴다.

## 4. T15 exec 세션

- **열림:** 공간 A의 T3 격리 시험이 PASS이고 도구용 Pod가 준비된 뒤, 시도마다 하나 연다. 세션은 `run_id`·`attempt`·공간 A UID를 가진다.
- **닫힘:** 제품 실행 종료, 세션을 연 뒤 180초(서버 측 만료. broker가 멈춰도 여기서 닫힌다), 통로 연결 끊김. 닫힌 뒤의 요청은 `REJECTED`·`UNAVAILABLE`·`ADMIT`다.
- **한도(ADR-0026, 실측 뒤 조정):** 동작 한 번 15초(재시도 포함), 시도당 실행 5회, exec 합계 90초(첫 호출부터). `EXECUTE` 단계에 들어간 호출만 횟수에 세고, `ADMIT`·`VALIDATE`에서 거부된 호출과 재시도는 세지 않는다. 넘으면 `BUDGET_EXHAUSTED`다.
- **재시도는 T15 안에서만 한다.** 미전송이 확인된 오류와 inspect·probe의 일시 실패만, 최대 2회, 간격 1초다. T11과 broker는 같은 `request_id` 재전송 외에는 재시도하지 않는다.
- **기록 수 상한:** 시도당 기록 32개다. 32번째를 기록하면 세션을 닫는다.

## 5. 응답

T15 → broker → T11 → 모델로 같은 객체가 간다(스키마 `response`). 키는 항상 모두 있고 해당 없는 값은 `null`이다.

| 필드 | 뜻 |
|---|---|
| `request_id`, `action` | 요청과 같은 값 |
| `result_code` | `OK` 실행함 · `REJECTED` 실행하지 않음 · `FAILED` 실행했거나 시도했으나 실패 · `UNKNOWN` 변경 반영 여부 모름 |
| `reason`, `stage` | 아래 표. `OK`이면 둘 다 `null`(출력을 막았을 때만 `OUTPUT_WITHHELD`·`OUTPUT`) |
| `status` | `OK`일 때 동작별 상태(아래), 그 밖에는 `null` |
| `output_tail`, `output_truncated` | 출력 끝부분 최대 2KiB(UTF-8 바이트, 글자 경계에서 자름). 잘랐으면 `true` |
| `budget` | 남은 실행 횟수(`calls_left`)와 남은 초(`seconds_left`) |

| `reason` | `result_code` | `stage` | 경우 |
|---|---|---|---|
| `BLOCKED` | REJECTED | ADMIT · VALIDATE · EXECUTE | 버전·digest 불일치, 공간 부재·UID 불일치, 인자 재검사 실패, 인자 안 비밀값, 파괴적 내용, API 권한 거부(실행 전 거부) |
| `FOREIGN_ATTEMPT` | REJECTED | ADMIT | 결속 값이 열린 세션과 다름 |
| `BUDGET_EXHAUSTED` | REJECTED | ADMIT | 횟수·시간 한도 초과 |
| `EFFECT_UNKNOWN` | UNKNOWN | EXECUTE | apply 요청이 넘어간 뒤 응답 유실·시간 초과·끊김 |
| `EFFECT_UNKNOWN` | REJECTED | ADMIT | 결과 모름 이후의 exec 호출 |
| `UNAVAILABLE` | FAILED | EXECUTE | 미전송 확인 오류(도구용 Pod 준비 전, 연결 거부)가 재시도 뒤에도 남음 |
| `UNAVAILABLE` | REJECTED | ADMIT | 세션이 닫힘, 또는 T11이 통로 장애로 보내지 못함 |
| `TIMEOUT` | FAILED | EXECUTE | inspect·probe가 재시도를 포함해 15초를 넘김 |
| `NONZERO_EXIT` | FAILED | EXECUTE | 동작이 끝까지 돌았고 실패를 돌려줌(API가 초안을 거부, 도구용 Pod 명령 실패) |
| `OUTPUT_WITHHELD` | OK | OUTPUT | 출력에서 리허설 비밀값 발견, UTF-8 아님, 검사 실패 |

- **동작별 `status`**
  - apply: `proposal_digest`, inspect로 볼 수 있는 대상 `targets`(종류·이름, 러너 템플릿이 정한 값, 최대 8개)
  - inspect: `found`, `phase`, `ready`, `replicas`, `ready_replicas`, `restarts`, `events`(이벤트 수). 대상에 맞지 않는 값은 `null`. 대상이 없으면 `found=false`이며 실패가 아니다.
  - probe: `probe_outcome`(`CONNECTED`·`REFUSED`·`TIMED_OUT`·`DNS_FAILED`), `http_status`(TCP면 `null`), `elapsed_ms`. 접속 거부·시간 초과·403도 시험 결과이므로 `OK`다.
- **출력:** apply는 실패 시 API 오류 본문, inspect는 이벤트·로그 본문, probe는 HTTP 응답 본문(HEAD·TCP는 없음)이다. 비밀값 검사는 자르기 전 전체 출력에 한다. 비밀값이 자르는 경계에 걸쳐 빠져나가지 않게 하기 위해서다. 막으면 `output_tail=null`, `output_truncated=false`다.
- **T11이 직접 만드는 응답:** 통로 장애일 때만 같은 형식으로 만든다. 보내는 중이던 apply는 `UNKNOWN`·`EFFECT_UNKNOWN`, inspect·probe는 `FAILED`·`UNAVAILABLE`, 이후 호출은 `REJECTED`·`UNAVAILABLE`이다. T15 응답이 스키마에 맞지 않을 때도 같은 규칙을 따른다.
- **T11의 제출 여유 확보:** 모델 1회 실행 한도(8턴·180초) 안에 제출할 턴이 남도록, T11은 남은 시간·턴을 보고 exec 도구를 먼저 닫을 수 있다. 닫은 뒤 들어온 호출은 T15로 보내지 않고 `REJECTED`·`BUDGET_EXHAUSTED`·`ADMIT`로 답한다.

## 6. 기록

두 목록을 따로 서명한다. 출처가 다르고, 실제 chatgpt-plan 경로에서는 T11 이벤트가 T15에 오지 않기 때문이다(`subscription_product.py`는 `tool_events_status=UNAVAILABLE`을 돌려준다).

| 목록 | 만드는 곳 | 시도 기록의 키 | T10 전달 | 내용 |
|---|---|---|---|---|
| T15 실행 기록 | T15 | `exec_events_status`, `exec_events` | `data.exec_events` | 무엇이 실행됐는지의 기준. ADR-0026 허용 키 |
| T11 감사 이벤트 | T11 | `tool_events_status`, `tool_events`(기존) | `data.tool_events`(기존) | 모델이 무엇을 불렀고 T11이 어떻게 처리했는지 |

**T15 `exec_events`** (스키마 `exec_event`, `exec_record`)
- `seq`마다 하나다. 실행 전 기록(순번·동작·검사된 인자·`args_sha256`)과 실행 뒤 기록(결과 코드·사유·출력 digest·크기)을 쓰기 1회로 남기고, 전달할 때 한 이벤트로 합친다. 실행 뒤 기록이 없으면 `UNKNOWN`·`EFFECT_UNKNOWN`·`EXECUTE`로 확정한다(ADR-0026 4-2).
- 공통 키: `run_id`, `attempt`, `seq`, `action`, `request_id`, `args_sha256`, `result_code`, `reason`, `stage`, `output_sha256`, `output_bytes`
- apply: `proposal_digest` · inspect: `target_kind`, `target_name`, `lines` · probe: `service`, `port`, `method`, `path`, `probe_outcome`, `http_status`
- 값은 문자열·정수·불리언만 쓴다. 해당 없는 키(성공의 `reason`, TCP의 `path`·`http_status`, 출력이 없을 때의 `output_*`)는 `null`로 두지 않고 뺀다. `collect_tool_events`와 같은 규칙이고, 목록 밖 키나 형식 오류가 하나라도 있으면 목록 전체가 `UNAVAILABLE`이다.
- `exec_events_status`: `OFF`(그 작업 종류는 exec를 껐음), `COMPLETE`, `UNAVAILABLE`. exec 단계 없이 돈 시도의 기록에는 이 키가 없으며 `OFF`로 읽는다(기존 기록 형식 유지).

**T11 `tool_events`의 exec 호출 (T11 확인 필요)**
- 기존 키 `run_id`, `attempt`, `tool`, `outcome`, `reason`을 유지하고, `tool`은 1절의 내부 이름을 쓴다.
- T11에서 거부하면 기존 사유를 쓴다: `INVALID_INPUT_OR_POLICY`, `DESTRUCTIVE_ATTEMPT_BLOCKED`(+`category`), `TOOL_NOT_PROVIDED`
- 통로로 보냈으면 `outcome=ALLOWED`, `reason=POLICY_ACCEPTED`에 `request_id`, `result_code`, `exec_reason`(T15 `reason`, 없으면 키를 뺌)을 더한다. 두 목록은 `request_id`로 대조한다.
- 인자 값(`path` 등)은 넣지 않는다. T11은 리허설 비밀값을 모르기 때문이다. 인자는 비밀값 검사를 거친 T15 기록에만 남는다.

**현재 코드와 대조**

| 항목 | 현재 | v1 |
|---|---|---|
| `collect_tool_events` 값 타입 | 문자열·정수·불리언만, 아니면 목록 전체 `UNAVAILABLE` | `exec_events`도 같은 규칙. `null` 금지 |
| 이벤트 결속 | `run_id == rehearsal_id`, `attempt == admission.attempt` | 같음. broker가 붙이는 `run_id`·`attempt`가 이 값이다 |
| 목록 상한 | `tool_events` 256개 | `exec_events` 32개 |
| T10 이벤트 스키마 | `data`는 형식 제한 없는 객체, `tool_events` 정의 없음 | `data.exec_events` 위치와 이벤트 종류는 T10에서 정한다 |

## 7. 버전과 변경 규칙

- 로컬 채널 메시지·T15 경로 요청·응답에 `schema_version: 1`을 싣는다. 모르는 버전이면 T15는 `REJECTED`·`BLOCKED`·`ADMIT`, T11은 5절의 통로 장애 규칙을 따른다.
- 도구 이름, 인자 스키마·필드 사이 규칙, 응답 필드, 사유 코드, 기록 키 가운데 하나라도 바꾸면 버전을 올린다. 같은 변경에서 T11 검사 규칙·T15 검사 규칙·이 문서·스키마 파일을 함께 바꾸고, T11·T15 담당이 모두 리뷰한다. 한쪽만 먼저 배포하지 않는다.
- 구현할 때 T11과 T15는 이 스키마 파일을 각자의 시험에서 읽어 자기 모델과 같은지 확인한다. 패키지에 사본을 두면 이 파일의 SHA-256을 함께 고정한다.

## 8. 남은 확인

| 항목 | 누구 | 상태 |
|---|---|---|
| 이 형식으로 T11 검사·도구 노출·`tool_events` 키를 구현해도 되는지 | 전동훈(T11) | 확인됨([11/6 02:41](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793900490783729?thread_ts=1793899865.617069&cid=C0CDEFEB83U)). T11 구현은 데모 필수 작업 뒤 |
| exec 사용 여부를 제품 실행 입력에 싣는 위치(T11이 도구 노출을 판단하는 근거). 제안: `ProductInput.exec_enabled`(불리언, 기본 `false`, 입력 digest에 포함) | 류지환(T38) | 미정 |
| `data.exec_events`를 싣는 T10 이벤트 종류와 T24·T21 표시 | 류지환(T10), 이상무(T21), 전동훈(T24) | 미정 |
| strict 모드에서 `pattern`·`minimum`·`["string","null"]`를 실제 모델 호출이 받는지 | 전동훈(T11) | 미확인. T11이 T38 경로로 한 번 호출해 결과를 남긴다. `plan_propose`의 `pattern`·`maxLength`는 실제 호출에서 쓰고 있다 |
| 자격 이전 작업의 공간 A 비밀값을 러너가 아는지. 모르면 그 작업 종류는 `OFF` | 전동훈(T9) | 확인됨: 러너가 합성 값을 OpenBao 리허설 경로에 직접 쓰므로 안다. 그 작업 종류도 exec를 켤 수 있다([11/6 02:41](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793900490783729?thread_ts=1793899865.617069&cid=C0CDEFEB83U)) |

## 보장하지 않는 것

- 출력 비밀값 검사는 러너가 공간 A에 넣은 값만 찾는다. 앱이 그 값을 변형(해시·부분 출력)해 내보내면 찾지 못한다. 그래서 공간 A에는 운영 비밀값을 넣지 않는다(ADR-0026).
- 응답의 `status`와 출력은 시험 참고 정보다. 채점은 공간 B의 검증 Job만 한다.
- 이 계약은 통로의 인증·암호화·연결 수명을 정하지 않는다. T38 broker 경로의 규칙을 따른다.
