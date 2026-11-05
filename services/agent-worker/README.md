# T11 — S2 실패 분석과 수정 제안

플랫폼 워커가 실패 근거와 승인된 앱 소스만 읽고 수정 제안서를 낸다. 현재 정본의 S2 기능으로 구현했다. 11/2 회의에서 역할 세분화는 열어 두었으며, 별도 서비스 도입은 실제 격리·배포 책임이 달라질 때 판단한다. GitHub의 저장소 CI, T15의 리허설 실행·비공개 검증, 사람 승인, T31 반영은 각각의 담당 경로에 남는다.

사용자의 11/3 요청에 따라 **관리자 ChatGPT 구독(SIWC)으로 OpenAI Responses를 호출하는 경로를 기본 제공자**로 구현했다. 정본 v0.3의 Claude Agent SDK 선택을 바꾸는 v0.4·[ADR-0007](../../docs/adr/0007-model-provider-and-s2-ci.md) 제안이며, 팀 전체가 이미 합의한 것으로 취급하지 않는다. 검증한 Claude SDK 어댑터는 선택 기능으로 보존한다. 자동 provider fallback과 Bedrock 연결은 없다.

| 모듈 | 책임 |
|---|---|
| `contracts.py` | 실패 입력·소스 목록·제안서·독립 검증 영수증의 Pydantic 계약 |
| `tools.py` | 정확한 소스 경로·내용 해시 확인, 제안 제출, 도구 차단·기록 |
| `coordinator.py` | run/revision/proposal/attempt 결속, 최대 3회, UNKNOWN 중단, 재시작용 상태 내보내기·복원. LLM import 없음 |
| `openai_provider.py` | OpenAI Responses 호출과 두 함수 도구. 기본 제공자 |
| `sdk.py` | 선택 설치하는 Claude Agent SDK 어댑터 |
| `runtime.py` | 전용 프로세스의 환경·모델 자격 선택 |
| `subscription_auth.py` | 앱 소유 SIWC 등록·ID 검증·갱신·회수 |
| `subscription_store.py` | 지정 VM 잠금·Secrets Manager 버전 결속 |
| `subscription_admin.py` | 관리자 등록·상태·모델 목록·구독 워커 실행 |
| `cli.py` | 플랫폼에서 한 번 실행하고 제안·감사 기록을 새 파일로 저장 |

도구는 정본의 화이트리스트 중 `repo.read`, `plan.propose`, `policy.evaluate`, `inventory.describe` 네 개를 연다. OpenAI 함수 이름은 `repo_read`, `plan_propose`, `policy_evaluate`, `inventory_describe`이며 Claude MCP 이름은 `mcp__bravo__` 접두사를 붙인 같은 이름이다. 모두 같은 정책 핸들러(`Tools.dispatch`)를 사용한다.

| 도구 | 하는 일 | 경계 |
|---|---|---|
| `repo.read` | manifest에 있는 정확한 경로의 snapshot 파일 읽기 | 경로 정책 + 내용 해시 + 비밀 패턴 거부 |
| `plan.propose` | 스키마에 맞는 제안서 1건 제출 | 파괴 명령 · wildcard 권한 · 비밀 패턴 거부, 수정 가능 경로만 |
| `policy.evaluate` | 후보 정책 · 교체 텍스트를 `plan.propose`와 같은 검사로 미리 린트 | 부작용 없음. `PASS`는 "이 검사에서 거부되지 않음"이지 안전 증명이 아니다 |
| `inventory.describe` | T16 인벤토리의 모델용 부분집합(항목 · 등급 · 코드 참조 위치) 읽기 | 제어 경로가 `InventorySummary`를 붙인 실행에서만 제공(`--inventory`, 아래 실행 절). snippet · 배포 속성 · 환경변수 값은 전달하지 않는다 |

`rehearsal.exec`, `rehearsal.run/result`는 아직 제공하지 않는다. 호출하면 거부되며 감사 사유는 `TOOL_NOT_PROVIDED`로 일반 미허용(`TOOL_NOT_ALLOWED`)과 구분한다. 어떤 도구든 인자에 버킷 삭제 · 네임스페이스 삭제 · DROP/TRUNCATE · rm -rf · terraform destroy 패턴이 있으면 사유 `DESTRUCTIVE_ATTEMPT_BLOCKED`와 코드가 정한 `category`(`bucket_delete` 등)만 기록하고 원문은 기록하지 않는다. 이것이 설계 8.4 데모 장면("정리를 이유로 한 버킷 삭제 시도 차단")의 감사 기록이다.

OpenAI 경로(API 키 · 구독 · `account_smoke`)에서도 같은 분류를 쓴다. 예약 도구는 모델에 제공하지 않지만 모델이 `rehearsal_exec` · `rehearsal.exec` · `bravo.rehearsal_exec` · `bravo.rehearsal.exec` · `mcp__bravo__rehearsal_exec`(run/result도 같음) 중 하나로 호출하면 `mcp__bravo__rehearsal_*`로 정규화해 `Tools.dispatch`의 거부 분류를 거친다. 그 밖의 이름은 `unlisted`(`TOOL_NOT_ALLOWED`)다. JSON이 아닌 인자도 분류만 하고 저장하지 않는다. 구독 경로에서 `bravo` 네임스페이스 밖의 호출과 첫 읽기 전의 다른 호출은 기존대로 그 시도를 끝내며(NEEDS_HUMAN), 끝내기 전에 제공 도구가 아닌 호출의 거부를 같은 방식으로 기록한다. 네임스페이스 밖의 `repo_read` 같은 제공 도구 이름은 실행하지 않고 `unlisted`로 기록한다. 응답 종료 이벤트의 output이 비어 스트림 항목으로 복원하는 경우에는 `bravo` 네임스페이스 안의 예약 이름만 복원 대상이다.

소스 경로 정책: 디렉터리 `secrets/` · `credentials/`, 키 파일(`.pem` · `.key` · `.p12` · `.tfstate`), 이름에 `credential` · `private_key`가 든 파일, 테스트 파일은 계속 거부한다. 이름에 `secret`이 든 **코드 파일**(`.py` · `.ts` · `.go` 등)은 읽을 수 있다. demo-chat의 `app/db_secret.py`가 Secrets Manager 직접 호출을 고쳐야 하는 바로 그 파일이기 때문이다. 내용은 여전히 `safe_text`를 거치므로 자격증명 리터럴이 들어 있으면 읽기가 거부된다(가리지 않고 거부한다: 가린 내용을 기준으로 제안하면 교체본과 원본 해시가 어긋난다).

## 실행

Python 3.12 이상, Linux/macOS의 `O_NOFOLLOW`·디렉터리 FD를 사용한다.

```sh
cd services/agent-worker
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run bravo-agent schema > /private/path/proposal.schema.json

# 먼저 아래 관리자 연결 절차로 구독을 등록한다.
# 운영자가 지정한 모델만 호출하고 API 키로 자동 전환하지 않는다.
uv run bravo-subscription run \
  --secret-arn "$SUBSCRIPTION_SECRET_ARN" \
  --runtime-host-id "$RUNTIME_HOST_ID" --state-dir "$BROKER_STATE_DIR" \
  --bundle /private/path/failure.json \
  --manifest /private/path/snapshot-manifest.json \
  --snapshot /private/path/read-only-app-snapshot \
  --model "$MODEL_ID" --output /private/path/new-proposal.json
```

### T16 인벤토리 입력 (FR-4)

`bravo-agent analyze`와 `bravo-subscription run`은 선택 인자 `--inventory /private/path/inventory.json`(T16 `bravo-inventory scan` 출력)을 받는다. 256 KiB까지만 읽고 `InventorySummary.from_document`로 모델용 부분집합만 남겨 `Coordinator`에 붙인다. 붙인 실행에서만 `inventory_describe`가 제공되고, 결과 파일에 `inventory_digest`(T16 문서의 `sha256:` digest)가 추가된다. 이 digest는 문서에 적힌 값을 그대로 믿지 않고, T16 `report.py`와 같은 규칙(`generated_at`·`digest`를 뺀 문서의 정렬 JSON의 sha256)으로 다시 계산해 같을 때만 받는다. 그래서 내용을 바꾼 문서는 원래 digest를 달고 들어올 수 없다. 값의 타입도 바꾸지 않고 그대로 엄격 검증한다. 붙이지 않으면 이 키는 없고 기존 출력과 같다. 인벤토리의 `app.source_sha`가 실패 요약·manifest와 다르거나, digest가 내용과 맞지 않거나, 형식이 맞지 않거나, 크기를 넘으면 다른 입력 오류와 같이 결과 파일 없이 `NEEDS_HUMAN`·종료 코드 2로 끝난다. 그래서 T16 scan은 같은 `--source-sha`로 실행해야 한다.

T10의 최초 제안 입력은 바꾸지 않는다(11/6 류지환 확인: `failure_code=CODE_CHANGE_REQUIRED`, `summary`=항목 `rationale`, `evidence_digest`=T16 문서 digest에서 `sha256:`를 뺀 값). T38 제품 경로(`ProductInput` · `invoke_product_worker`)는 인벤토리를 아직 전달하지 않는다. `run_worker`는 `args.inventory`가 없으면 기존 명령을 그대로 만든다. 제품 경로에 붙일 때의 제안 필드는 [ADR-0019 4절](../../docs/adr/0019-agent-tool-boundary.md#4-결과-consequences)에 적었고 T38·T10 담당 합의 전에는 구현하지 않는다.

관리자 등록·최소 AWS 권한·갱신·회수 절차는 [구독 연결 운영 가이드](subscription.md)를 따른다. 기본 `bravo-agent analyze`도 `chatgpt-plan`을 선택하며 호출 프로세스의 `ACCESS_TOKEN`을 요구한다. 별도 API 청구를 운영자가 명시적으로 선택할 때만 `--provider openai`와 `OPENAI_API_KEY`를 쓴다. 이 PR의 구독 실행은 API 키를 사용하지 않는다.

Claude 어댑터는 `uv sync --frozen --extra claude`로 설치하고 `--provider claude`를 명시하며 플랫폼의 `ANTHROPIC_API_KEY`만 전달한다. 기본 production 이미지에는 Claude CLI를 포함하지 않는다.

### 팀 공통 인증과 공유 파일

- AWS 자동 실행은 기존 GitHub Actions OIDC 역할을 사용한다. 역할·실행 대상 참조는 팀 GitHub Secrets에 두며 장기 AWS 액세스 키를 만들지 않는다. T7의 고정 검증 역할을 모델 키 조회나 임의 실행 권한으로 확대하지 않는다.
- 관리자는 Bravo 앱 소유의 SIWC 로그인에 동의한다. 발급된 client ID·검증된 계정 결속·토큰 묶음은 팀 전용 Secrets Manager 항목에 저장한다. 지정 VM의 인증 담당 프로세스만 갱신 토큰과 AWS 권한을 갖고, 모델 자식 프로세스에는 접근 토큰 하나만 전달한다. 앱 소스·리허설 guest·일반 PR CI에 인증을 전달하지 않는다. 기존 `PROJECT_TOKEN`·`SLACK_BOT_TOKEN`·DB 시크릿·개인 Codex 캐시는 재사용하지 않는다. OIDC는 AWS 권한을 얻는 통로이며 구독 동의를 대신하지 않는다.
- `.gitignore`는 로컬 Terraform 입력·상태, `.env`, AWS/Kubernetes/개인 CLI 인증과 비공개 협업 기록을 제외한다. 예시 입력, 공통 코드·설정, 검토된 예산 정책과 개인정보를 제거한 검증 기록은 커밋한다. 이미 추적된 파일의 비밀은 ignore 추가만으로 제거되지 않으므로 공개 증빙 검사와 자격 탐지를 별도로 수행한다.
- 아래 `account_smoke`는 사용자가 직접 인증하는 과거 일회성 진단이다. 공통 실행은 별도 SIWC 등록을 사용하며 개인 Codex OAuth·`auth.json`을 복사하지 않는다.

실행 결과 `PROPOSAL_READY`는 스키마·로컬 정책을 통과한 제안이 생겼다는 뜻이다. 실제 리허설 성공이나 승인이 아니다. 모델·입력·정책 실패는 `NEEDS_HUMAN`과 종료 코드 2다. 출력 파일을 덮어쓰지 않는다. 예시 파일은 `examples/`의 **합성 입력**이며 실계정 실행 결과가 아니다.

`failure.json`은 T15가 만든 민감정보 없는 실패 요약이다. `snapshot-manifest.json`의 `source_sha`, 파일별 SHA-256, `editable_paths`는 신뢰된 수집기가 고정한다. 모델이 정하지 않는다. 이 모듈은 파일 바이트와 manifest의 SHA-256 일치를 검사한다. Git 커밋 SHA와 snapshot을 연결해 발급하는 수집기는 T10/T15 통합 지점으로 남아 있다. 임의 manifest를 신뢰할 수 있는 커밋 증명으로 취급하면 안 된다.

## 도구와 입력 경계

- OpenAI에는 strict 함수 도구 두 개만 제공하고 built-in shell·web·computer·code interpreter 도구는 제공하지 않는다. 모든 호출을 정확한 이름으로 분기하고 정책 핸들러에서 다시 검사한다. `parallel_tool_calls=False`, `store=False`, `max_retries=0`이며 API endpoint는 `https://api.openai.com/v1`로 고정한다.
- Claude 선택 경로는 `tools=[]`로 내장 도구를 제거한다. `allowed_tools`만으로는 배타적 화이트리스트가 되지 않는다. 모든 도구 호출의 `PreToolUse` 훅과 MCP 핸들러가 각각 검사한다. MCP 하나, `strict_mcp_config=True`, `setting_sources=[]`, `skills=[]`, 빈 플러그인·서브에이전트 설정, `dontAsk`, 실행별 빈 SDK 작업 디렉터리, 자동 메모리 끔을 사용한다.
- SDK의 `env`는 부모 환경을 덮어쓰는 옵션일 뿐 대체하지 않는다. 전용 CLI 프로세스에서 PATH·locale·**선택한 제공자의** 모델 자격만 선택하고 HOME·설정 경로를 실행별 임시 디렉터리로 교체한다. AWS·GitHub·kubeconfig·개인 OAuth·다른 제공자의 키·사용자 지정 API endpoint 환경값은 전달하지 않는다. 라이브러리 호출은 공유 프로세스의 환경을 바꾸지 않으며, 격리되지 않은 환경에서 모델 호출을 거부한다.
- 파일 목록은 최대 64개, 한 파일 32 KiB, 수정 가능 경로 최대 8개다. 정확한 manifest 경로만 읽으며 경로 탈출·숨김/테스트/자격증명 경로·심볼릭 링크·하드링크·변경된 바이트를 거부한다. 모델 입력에 비공개 테스트나 원시 운영 로그를 넣지 않는다.
- 제안서는 최대 4개의 파일 교체와 이유·위험도만 포함한다. 실행 명령·판정·배포 필드는 스키마에서 거부한다. 알려진 파괴적 명령과 wildcard 권한도 제안 도구에서 차단한다. 이는 임의 코드의 안전성 증명이 아니다. 의미상 권한 약화·검증 우회는 독립 정책 검사와 T15 비공개 검증이 추가로 판단해야 한다.
- 알려진 키·비밀 패턴을 입력/출력에서 거부한다. 모든 비밀을 탐지한다는 보장은 없다. 수집기는 비밀 검사를 통과한 앱 소스와 정리된 실패 근거만 넘겨야 한다. 모델 키는 API 호출 프로세스에만 주입하며 앱 snapshot과 리허설 guest에 넣지 않는다.

IAM 정책의 전체 허용(`*`) 검사는 JSON 구조를 읽는다. 각 Statement의 정확한 `Effect`가
`Deny`이면 `Principal: "*"` 등을 사용한 거부 정책을 허용한다. `Allow`에서는
Action·Resource 배열의 모든 원소와 Principal 객체 안의 배열까지 검사해 값 `"*"`를
거부하며, 보완 집합을 허용하는 NotAction·NotResource·NotPrincipal도 거부한다.
다른 Statement의 Deny나 Condition은 이 검사의 예외가 되지 않는다. JSON 설정 안에
중첩되거나 문자열로 직렬화된 정책도 같은 검사를 받는다. 중복 키, 누락·잘못된 Effect,
모호한 형식과 64단계를 넘는 중첩은 거부·기록한다. 정책으로 인식되는 Python·YAML·HCL
등의 소스 문법은 이 경계에서 해석하지 않고 거부하므로, 정책 변경은 완전한 JSON으로
제출해야 한다. 이는 IAM 전체 문법·유효 권한 검사기가 아니며 `s3:Get*`와 범위를 지정한
리소스 패턴, 임의 코드의 정책 생성·권한 약화는 T15의 독립 검증에서 확인해야 한다.
근거: [AWS 정책 문법](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_grammar.html),
[Effect](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_effect.html),
[Principal](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_principal.html).

자유 텍스트 모델 응답은 제안서로 채택하지 않는다. `plan.propose` 제출만 수집한다. 한 시도는 모델 호출 최대 8회·180초이며 핸들러는 도구 호출 64회에서 중단한다. OpenAI는 요청 본문 128 KiB로 제한하고, API가 보고한 누적 사용량 32,000토큰을 넘으면 제안을 채택하지 않고 중단한다. 사용량은 응답 후 알려지므로 마지막 호출의 초과 비용을 되돌리지는 못한다. 달러 비용의 강제 상한으로 표현하지 않는다. 구독 경로는 `store=false`, `stream=true`를 강제하고 `response.completed`만 성공으로 본다. 구독에서 지원하지 않는 `max_output_tokens`는 보내지 않으며 스트림은 2 MiB에서 중단한다. 명시적인 API 키 경로만 호출당 출력 4,096토큰 제한을 쓴다. 구독 한도나 일시 오류는 자동 재로그인·초기화·크레딧 추가·API 전환 없이 중단한다. Claude 선택 경로는 SDK `max_budget_usd=0.5`도 지정한다. 모델 오류나 알 수 없는 효과는 자동으로 다시 실행하지 않는다.

## T15와의 연결

신뢰된 제어 경로가 `Coordinator.propose(provider)` 결과를 T15로 넘긴다. 모델에는 `Coordinator.verify()` 호출 도구가 없다. T15의 영수증은 `run_id`, `source_sha`, `proposal_digest`, `attempt`, `evidence_digest`를 결속한다.

| 독립 결과 | 다음 상태 |
|---|---|
| 결속 일치, PASS, 효과 확인 | `AWAITING_APPROVAL` — 승인·반영은 별도 |
| 결속 일치, FAIL, 효과 확인, 시도 1~2 | `RETRY_ALLOWED` — 새 실패 요약을 다음 시도에 전달. 모델 입력은 `task`(원 작업: 목적 · 수정 가능 파일) + `failure`(최신 검증 피드백) + `attempt` + `snapshot`이며 재시도에서도 `task`는 바뀌지 않는다(`Tools.model_input`) |
| FAIL 3회, UNKNOWN, 효과 미확인, 결속 불일치 | `NEEDS_HUMAN` |

실패 영수증에는 같은 run/revision/evidence에 결속된 새 `FailureBundle`이 필요하다. Coordinator는 원 작업 번들을 `task`로 보존하고 결과 · 각 시도에 `task_digest`를 남긴다. 러너가 재시작하면 확인되지 않은 시도를 자동 재실행하지 않고 사람에게 넘긴다(아래 [재시작과 상태 보존](#재시작과-상태-보존), 저장은 T10). 각 시도의 정리된 입력·제안과 검증 영수증을 결과에 보존한다. 감사 이벤트에는 코드가 정한 도구명·사유·결과만 저장하고 거부된 원시 인자, provider 오류 문자열과 stderr는 저장하지 않는다.

허용된 소스 읽기는 경로와 내용 해시를 남긴다. 감사 이벤트가 시도당 128개를 넘으면 초과 여부와 전체 건수를 표시하고 `NEEDS_HUMAN`으로 중단한다. 기록이 잘린 제안을 승인 대기로 넘기지 않는다.

### 재시작과 상태 보존

coordinator는 프로세스 안의 객체지만 상태를 문서로 내보내고 새 프로세스에서 이어 갈 수 있다. 저장·잠금은 T10/T15 몫이고 T11은 API와 검사만 제공한다.

- `Coordinator(..., checkpoint=fn)`: 상태가 바뀔 때마다 `fn(export_state())`를 부른다. 모델 호출 **전**에 `ANALYZING` 상태를 먼저 넘긴다(write-ahead). 이 저장이 실패하면 모델을 부르지 않고 `NEEDS_HUMAN`(`STATE_CHECKPOINT_FAILED`)으로 멈춘다. 호출 뒤·영수증 처리 뒤 저장 실패도 같은 사유로 멈춘다.
- `Coordinator.export_state() -> dict`: JSON으로 그대로 저장할 수 있는 문서. `state_digest`는 나머지 필드의 T11 `digest`(정렬 키·구분자 고정 JSON의 SHA-256)다.
- `Coordinator.restore(state, snapshot, inventory=None, checkpoint=None)`: 필드 집합·`state_digest`·버전·run/revision 결속·snapshot·inventory digest·시도 기록 수·영수증의 시도 번호·상태와 기록의 일치를 확인한다. 하나라도 어긋나면 `StateRejected`이며 호출자는 새로 시작하지 않고 사람에게 넘긴다.
- 복원 규칙: `ANALYZING`(모델 호출 중이라 결과 모름)은 `NEEDS_HUMAN`으로 바꾸고 그 시도를 쓴 것으로 센다(`RESTART_OUTCOME_UNKNOWN`). 다시 실행하지 않는다. 시도 수는 문서의 `attempt`를 이어 받으므로 재시작해도 합계 3회를 넘지 않고, 3회째의 `RETRY_ALLOWED` 문서는 거부한다. 복원한 `PROPOSAL_READY`의 `verify()`는 같은 `proposal_digest`·`attempt`·`run_id` 영수증만 받는다.
- `bravo-agent analyze --state-output <새 파일>`은 같은 checkpoint를 임시 파일 + `os.replace`로 남긴다. 기존 파일이 있으면 시작하지 않는다.

T10/T15가 저장할 필드(`schema_version` 1, `kind=bravo.t11.coordinator_state`):

| 필드 | 뜻 |
|---|---|
| `run_id`, `source_sha` | 원 작업의 결속 값 |
| `snapshot_digest`, `inventory_digest` | 복원할 때 넘기는 snapshot manifest와 T16 요약의 digest(없으면 `null`) |
| `revision` | 상태 전이마다 1 증가. 저장소는 `revision`이 정확히 1 큰 문서만 받는 비교 후 쓰기로 동시 실행·오래된 쓰기를 막는다 |
| `attempt`, `state` | 쓴 시도 수(0~3)와 `READY`·`ANALYZING`·`PROPOSAL_READY`·`RETRY_ALLOWED`·`AWAITING_APPROVAL`·`NEEDS_HUMAN` |
| `task`, `bundle` | 원 작업 `FailureBundle`과 최신 검증 피드백 |
| `proposal`, `proposal_digest` | 마지막 제안서와 digest |
| `attempts`, `audit` | 시도별 정리된 입력·제안·영수증, 코드가 정한 감사 이벤트 |
| `state_digest` | 위 필드 전체의 digest |

`state_digest`는 손상·부분 수정 탐지이며 서명이 아니다. digest를 다시 계산해 넣은 문서는 내용 규칙(시도 수와 기록 수, 3회 상한, 상태와 기록 일치)으로만 막힌다. 위조 방지·단일 실행 권한·되돌리기 금지는 T10 저장소의 접근 제어와 `revision` 비교로 보장해야 한다. 문서에는 모델 자격·원시 인자·provider 오류 문자열이 없다(결과 파일과 같은 내용). T10 저장·T15 영수증의 인증된 전달과 실제 재시작 운영은 연결하지 않았다. CLI는 최초 제안 한 번만 수행하며 재시도 실행 경로는 T10 제어 경로가 `restore()`로 연다. T15의 실제 Job 실행, hidden-test 격리, 승인 소비와 T31 반영은 구현하지 않았다.

## 플랫폼 배포 조건과 현재 증거

워커는 플랫폼에서 비루트 사용자·읽기 전용 root filesystem으로 실행한다. `/tmp`만 실행별 임시 쓰기를 허용하고 snapshot은 읽기 전용 볼륨으로 준다. 서비스 계정 token 자동 마운트, hostPath, host network, privileged, 운영 kubeconfig·DB 자격증명을 금지한다. CPU·메모리·시간 한도를 두고 모델 endpoint로의 송신만 허용해야 한다. 리허설 guest와 검증 Job은 T3/T15의 별도 격리 공간이다.

`Dockerfile`은 잠긴 의존성을 설치하고 UID/GID 10001로 CLI를 실행한다. 실행할 때 `--read-only`, `/tmp`의 제한된 tmpfs, 입력 볼륨 `:ro`, 별도 결과 볼륨, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, PID·CPU·메모리 한도를 적용해야 한다. Kubernetes Job의 구체적인 namespace·이미지 digest·Secret 참조·송신 정책은 T7 플랫폼 배포에서 결속한다. 임의 Secret 이름이나 열린 네트워크 정책을 기본 배포로 추가하지 않았다.

로컬 시험은 설치된 OpenAI SDK 3.42.1·Claude Agent SDK 0.2.177과 가짜 API/provider를 사용하는 기능/공격 입력 검증이다. OpenAI 함수 스키마·요청 옵션, Claude CLI 인자 생성도 확인한다. 로컬 시험만으로 실제 모델·클라우드 동작을 검증했다고 주장하지 않는다. 별도 [AWS 실행 기록](../../docs/evidence/t11-ci-2026-11-03/README.md)에는 같은 코드의 74개 시험, arm64 이미지 빌드, 제한된 컨테이너 5개 시나리오 결과가 있다. 별도 [공식 CLI 계정 검증](../../docs/evidence/t11-ci-2026-11-03/openai-account-local.md)에서는 기존 ChatGPT 로그인으로 실제 모델 응답을 받아 제안서 정책을 통과했다. 이후 [AWS 구독 진단](../../docs/evidence/t11-account-cloud-2026-11-03/README.md)에서 실제 모델의 읽기·제안 도구와 제안서 수집도 확인했다. 이후 [관리자 SIWC와 Secrets 경로](../../docs/evidence/t11-subscription-2026-11-03/README.md)에서도 AWS 실제 제안 2회와 재로그인 없는 세션 재사용, 로컬·AWS 177개 시험을 확인했다. 일반 API 키 인증·실제 갱신/회수·GCP·상시 무인 배포·T15 검증 Job은 별도 미검증이다. T7 런타임·T15 검증 경로와 통합하기 전에는 T11 전체 완료로 체크하지 않는다.

근거: [팀 설계 8.3](../../docs/design.html#s8-3), [T11·T15 작업 계획](../../docs/tasks.md), [OpenAI 함수 호출](https://developers.openai.com/api/docs/guides/function-calling), [Responses 상태 보관](https://developers.openai.com/api/docs/guides/migrate-to-responses), [Claude SDK Python 문서](https://code.claude.com/docs/en/agent-sdk/python), [Claude SDK 권한 평가 순서](https://code.claude.com/docs/en/agent-sdk/permissions). SDK 버전과 전이 의존성은 `uv.lock`에 고정했다.

## 운영자가 직접 수행하는 구독 계정 검증

`account_smoke`는 사용자가 요청한 개인 구독 계정 검증을 위한 명시적 진입점이다. 기본 공통 실행과 분리된 진단이다. 공식 Codex CLI 0.157.0의 장치 로그인을 사용하며, 다른 앱에 개인 계정 접근을 제공하는 호스팅 서비스나 무인 운영 인증으로 사용하지 않는다. [공식 인증 문서](https://learn.chatgpt.com/docs/auth#login-on-headless-devices), [app-server 인증 범위](https://learn.chatgpt.com/docs/app-server#auth-endpoints)

```sh
uv run python -m bravo_agent.account_smoke \
  --codex /absolute/path/to/verified/codex \
  --bundle examples/failure.json \
  --manifest examples/snapshot-manifest.json \
  --snapshot examples/source \
  --model "$MODEL_ID" \
  --output /private/path/new-account-result.json
```

설치자는 공식 0.157.0 릴리스의 플랫폼별 SHA-256을 확인한 `codex`와 `codex-code-mode-host`를 같은 디렉터리에 제공한다. 두 배포 압축 파일의 해시와 압축 해제한 실행 파일의 해시는 구분해 기록한다. 검증기는 로그인 전에 두 파일의 실행 가능 여부를 검사하며, 보조 파일이 없으면 `ACCOUNT_INSTALLATION_INCOMPLETE`로 중단한다. 이 사전 검사는 설치자의 릴리스 해시 검증을 대신하지 않는다. 실행은 별도 비루트 사용자와 읽기 전용 시스템 파일, 실행별 임시 쓰기 공간, CPU·메모리·시간 제한을 적용한 신뢰된 호스트에서만 한다. 이 명령 자체가 호스트 격리를 구성하지는 않는다.

- CLI의 인증 저장소는 `ephemeral`이며 같은 프로세스의 메모리에만 인증을 유지한다. 개발자 인증 캐시·API 키·AWS 자격·사용자 설정을 복사하지 않는다. 인증 파일이 생성되면 실패한다. 장치 로그인 대기는 최대 10분이고 인증 뒤 제안 실행은 최대 180초다.
- 빈 실행 디렉터리, 환경 접근 없음, `read-only`·승인 거부를 설정한다. shell·apps·plugins·hooks·멀티에이전트·브라우저·이미지·외부 MCP를 제공하지 않는다. 모델에는 `repo_read`, `plan_propose`만 등록하고 기존 `Tools` 정책으로 다시 검사한다. 예상 밖 내장 작업이나 승인 요청은 실행을 종료한다.
- Codex 0.157.0에서 동적 도구 전달에 필요한 `code_mode_host`는 켠다. 모델의 Code Mode 도구와 셸 실행은 계속 끈다. 이 호스트까지 끄면 인증·모델 응답은 가능해도 두 정책 도구가 실행되지 않아 제안 없이 중단된다. 자유 텍스트 응답이나 사용량 수치만으로 제안 성공을 판정하지 않는다.
- 모델을 명시하고 자동 대체를 허용하지 않는다. 도구 호출 64회, 보고된 누적 사용량 32,000토큰에서 중단한다. CLI 내부 요청·재시도 횟수는 Responses 제공자의 8회 제한과 같다고 주장하지 않는다. 오류 알림이 오면 중단하고 자동으로 새 시도를 만들지 않는다.
- 계정 인증 성공, 모델 응답 성공, 정책을 통과한 제안서를 별도로 확인한다. `usageLimitExceeded` 등은 원문·계정 정보 없이 허용된 오류 분류만 기록하고 `NEEDS_HUMAN`으로 종료한다. 이 결과는 T15 검증·승인·배포 성공이 아니다.

실측 결과와 실행 소스 결속은 [클라우드 계정 검증 기록](../../docs/evidence/t11-account-cloud-2026-11-03/README.md)을 따른다.
