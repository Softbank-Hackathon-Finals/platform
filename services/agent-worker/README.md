# T11 — S2 실패 분석과 수정 제안

플랫폼 워커가 실패 근거와 승인된 앱 소스만 읽고 수정 제안서를 낸다. 현재 정본의 S2 기능으로 구현했다. 11/2 회의에서 역할 세분화는 열어 두었으며, 별도 서비스 도입은 실제 격리·배포 책임이 달라질 때 판단한다. GitHub의 저장소 CI, T15의 리허설 실행·비공개 검증, 사람 승인, T31 반영은 각각의 담당 경로에 남는다.

사용자의 11/3 요청에 따라 **OpenAI Responses API를 기본 제공자**로 구현했다. 정본 v0.3의 Claude Agent SDK 선택을 바꾸는 v0.4·[ADR-0007](../../docs/adr/0007-model-provider-and-s2-ci.md) 제안이며, 팀 전체가 이미 합의한 것으로 취급하지 않는다. 검증한 Claude SDK 어댑터는 선택 기능으로 보존한다. 자동 provider fallback과 Bedrock 연결은 없다.

| 모듈 | 책임 |
|---|---|
| `contracts.py` | 실패 입력·소스 목록·제안서·독립 검증 영수증의 Pydantic 계약 |
| `tools.py` | 정확한 소스 경로·내용 해시 확인, 제안 제출, 도구 차단·기록 |
| `coordinator.py` | run/revision/proposal/attempt 결속, 최대 3회, UNKNOWN 중단. LLM import 없음 |
| `openai_provider.py` | OpenAI Responses 호출과 두 함수 도구. 기본 제공자 |
| `sdk.py` | 선택 설치하는 Claude Agent SDK 어댑터 |
| `runtime.py` | 전용 프로세스의 환경·모델 키 선택 |
| `cli.py` | 플랫폼에서 한 번 실행하고 제안·감사 기록을 새 파일로 저장 |

도구는 정본의 화이트리스트 중 `repo.read`, `plan.propose` 두 개만 연다. OpenAI 함수 이름은 `repo_read`, `plan_propose`이며 Claude MCP 이름은 `mcp__bravo__repo_read`, `mcp__bravo__plan_propose`다. 같은 정책 핸들러를 사용한다. 아직 제공하지 않는 `inventory.describe`, `rehearsal.exec`, `rehearsal.run/result`, `policy.evaluate`를 동작한다고 표시하지 않는다.

## 실행

Python 3.12 이상, Linux/macOS의 `O_NOFOLLOW`·디렉터리 FD를 사용한다.

```sh
cd services/agent-worker
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run bravo-agent schema > /private/path/proposal.schema.json

# 모델 키는 플랫폼의 secret 주입으로 OPENAI_API_KEY에 제공한다.
# 개인 Codex/Claude OAuth 로그인이나 개발자 홈 디렉터리를 복사하지 않는다.
uv run bravo-agent analyze \
  --bundle /private/path/failure.json \
  --manifest /private/path/snapshot-manifest.json \
  --snapshot /private/path/read-only-app-snapshot \
  --model "$MODEL_ID" \
  --output /private/path/new-proposal.json
```

Claude 어댑터는 `uv sync --frozen --extra claude`로 설치하고 `--provider claude`를 명시하며 플랫폼의 `ANTHROPIC_API_KEY`만 전달한다. 기본 production 이미지에는 Claude CLI를 포함하지 않는다.

실행 결과 `PROPOSAL_READY`는 스키마·로컬 정책을 통과한 제안이 생겼다는 뜻이다. 실제 리허설 성공이나 승인이 아니다. 모델·입력·정책 실패는 `NEEDS_HUMAN`과 종료 코드 2다. 출력 파일을 덮어쓰지 않는다. 예시 파일은 `examples/`의 **합성 입력**이며 실계정 실행 결과가 아니다.

`failure.json`은 T15가 만든 민감정보 없는 실패 요약이다. `snapshot-manifest.json`의 `source_sha`, 파일별 SHA-256, `editable_paths`는 신뢰된 수집기가 고정한다. 모델이 정하지 않는다. 이 모듈은 파일 바이트와 manifest의 SHA-256 일치를 검사한다. Git 커밋 SHA와 snapshot을 연결해 발급하는 수집기는 T10/T15 통합 지점으로 남아 있다. 임의 manifest를 신뢰할 수 있는 커밋 증명으로 취급하면 안 된다.

## 도구와 입력 경계

- OpenAI에는 strict 함수 도구 두 개만 제공하고 built-in shell·web·computer·code interpreter 도구는 제공하지 않는다. 모든 호출을 정확한 이름으로 분기하고 정책 핸들러에서 다시 검사한다. `parallel_tool_calls=False`, `store=False`, `max_retries=0`이며 API endpoint는 `https://api.openai.com/v1`로 고정한다.
- Claude 선택 경로는 `tools=[]`로 내장 도구를 제거한다. `allowed_tools`만으로는 배타적 화이트리스트가 되지 않는다. 모든 도구 호출의 `PreToolUse` 훅과 MCP 핸들러가 각각 검사한다. MCP 하나, `strict_mcp_config=True`, `setting_sources=[]`, `skills=[]`, 빈 플러그인·서브에이전트 설정, `dontAsk`, 실행별 빈 SDK 작업 디렉터리, 자동 메모리 끔을 사용한다.
- SDK의 `env`는 부모 환경을 덮어쓰는 옵션일 뿐 대체하지 않는다. 전용 CLI 프로세스에서 PATH·locale·**선택한 제공자의** 모델 API 키만 선택하고 HOME·설정 경로를 실행별 임시 디렉터리로 교체한다. AWS·GitHub·kubeconfig·개인 OAuth·다른 제공자의 키·사용자 지정 API endpoint 환경값은 전달하지 않는다. 라이브러리 호출은 공유 프로세스의 환경을 바꾸지 않으며, 격리되지 않은 환경에서 모델 호출을 거부한다.
- 파일 목록은 최대 64개, 한 파일 32 KiB, 수정 가능 경로 최대 8개다. 정확한 manifest 경로만 읽으며 경로 탈출·숨김/테스트/자격증명 경로·심볼릭 링크·하드링크·변경된 바이트를 거부한다. 모델 입력에 비공개 테스트나 원시 운영 로그를 넣지 않는다.
- 제안서는 최대 4개의 파일 교체와 이유·위험도만 포함한다. 실행 명령·판정·배포 필드는 스키마에서 거부한다. 알려진 파괴적 명령과 wildcard 권한도 제안 도구에서 차단한다. 이는 임의 코드의 안전성 증명이 아니다. 의미상 권한 약화·검증 우회는 독립 정책 검사와 T15 비공개 검증이 추가로 판단해야 한다.
- 알려진 키·비밀 패턴을 입력/출력에서 거부한다. 모든 비밀을 탐지한다는 보장은 없다. 수집기는 비밀 검사를 통과한 앱 소스와 정리된 실패 근거만 넘겨야 한다. 모델 키는 API 호출 프로세스에만 주입하며 앱 snapshot과 리허설 guest에 넣지 않는다.

자유 텍스트 모델 응답은 제안서로 채택하지 않는다. `plan.propose` 제출만 수집한다. 한 시도는 모델 호출 최대 8회·180초이며 핸들러는 도구 호출 64회에서 중단한다. OpenAI는 요청 본문 128 KiB·호출당 출력 4,096토큰으로 제한하고, API가 보고한 누적 사용량 32,000토큰을 넘으면 제안을 채택하지 않고 중단한다. 사용량은 응답 후 알려지므로 마지막 호출의 초과 비용을 되돌리지는 못한다. 달러 비용의 강제 상한으로 표현하지 않는다. Claude 선택 경로는 SDK `max_budget_usd=0.5`도 지정한다. 모델 오류나 알 수 없는 효과는 자동으로 다시 실행하지 않는다.

## T15와의 연결

신뢰된 제어 경로가 `Coordinator.propose(provider)` 결과를 T15로 넘긴다. 모델에는 `Coordinator.verify()` 호출 도구가 없다. T15의 영수증은 `run_id`, `source_sha`, `proposal_digest`, `attempt`, `evidence_digest`를 결속한다.

| 독립 결과 | 다음 상태 |
|---|---|
| 결속 일치, PASS, 효과 확인 | `AWAITING_APPROVAL` — 승인·반영은 별도 |
| 결속 일치, FAIL, 효과 확인, 시도 1~2 | `RETRY_ALLOWED` — 새 실패 요약을 다음 시도에 전달 |
| FAIL 3회, UNKNOWN, 효과 미확인, 결속 불일치 | `NEEDS_HUMAN` |

실패 영수증에는 같은 run/revision/evidence에 결속된 새 `FailureBundle`이 필요하다. 각 시도의 정리된 입력·제안과 검증 영수증을 결과에 보존한다. 감사 이벤트에는 코드가 정한 도구명·사유·결과만 저장하고 거부된 원시 인자, provider 오류 문자열과 stderr는 저장하지 않는다.

허용된 소스 읽기는 경로와 내용 해시를 남긴다. 감사 이벤트가 시도당 128개를 넘으면 초과 여부와 전체 건수를 표시하고 `NEEDS_HUMAN`으로 중단한다. 기록이 잘린 제안을 승인 대기로 넘기지 않는다.

이 coordinator는 프로세스 안의 상태다. T10은 동일 run의 단일 실행 권한·시도 수·이전 영수증을 영속화해야 하며 프로세스를 새로 띄워 시도 수를 초기화하면 안 된다. CLI는 최초 제안 한 번만 수행한다. T15의 실제 Job 실행, hidden-test 격리, 영수증의 인증된 전달·영속 저장, 승인 소비와 T31 반영은 구현하지 않았다.

## 플랫폼 배포 조건과 현재 증거

워커는 플랫폼에서 비루트 사용자·읽기 전용 root filesystem으로 실행한다. `/tmp`만 실행별 임시 쓰기를 허용하고 snapshot은 읽기 전용 볼륨으로 준다. 서비스 계정 token 자동 마운트, hostPath, host network, privileged, 운영 kubeconfig·DB 자격증명을 금지한다. CPU·메모리·시간 한도를 두고 모델 endpoint로의 송신만 허용해야 한다. 리허설 guest와 검증 Job은 T3/T15의 별도 격리 공간이다.

`Dockerfile`은 잠긴 의존성을 설치하고 UID/GID 10001로 CLI를 실행한다. 실행할 때 `--read-only`, `/tmp`의 제한된 tmpfs, 입력 볼륨 `:ro`, 별도 결과 볼륨, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, PID·CPU·메모리 한도를 적용해야 한다. Kubernetes Job의 구체적인 namespace·이미지 digest·Secret 참조·송신 정책은 T7 플랫폼 배포에서 결속한다. 임의 Secret 이름이나 열린 네트워크 정책을 기본 배포로 추가하지 않았다.

로컬 시험은 설치된 OpenAI SDK 3.42.1·Claude Agent SDK 0.2.177과 가짜 API/provider를 사용하는 기능/공격 입력 검증이다. OpenAI 함수 스키마·요청 옵션, Claude CLI 인자 생성도 확인한다. 로컬 시험만으로 실제 모델·클라우드 동작을 검증했다고 주장하지 않는다. 별도 [AWS 실행 기록](../../docs/evidence/t11-ci-2026-11-03/README.md)에는 같은 코드의 74개 시험, arm64 이미지 빌드, 제한된 컨테이너 5개 시나리오 결과가 있다. 별도 [공식 CLI 계정 검증](../../docs/evidence/t11-ci-2026-11-03/openai-account-local.md)에서는 기존 ChatGPT 로그인으로 실제 모델 응답을 받아 제안서 정책을 통과했다. 이는 로컬 운영자 검증이며, 이 워커의 Responses API 인증·클라우드 모델 호출·GCP·운영 배포·T15 검증 Job은 미검증이다. T7 런타임·T15 검증 경로와 통합하기 전에는 T11 전체 완료로 체크하지 않는다.

근거: [팀 설계 8.3](../../docs/design.html#s8-3), [T11·T15 작업 계획](../../docs/tasks.md), [OpenAI 함수 호출](https://developers.openai.com/api/docs/guides/function-calling), [Responses 상태 보관](https://developers.openai.com/api/docs/guides/migrate-to-responses), [Claude SDK Python 문서](https://code.claude.com/docs/en/agent-sdk/python), [Claude SDK 권한 평가 순서](https://code.claude.com/docs/en/agent-sdk/permissions). SDK 버전과 전이 의존성은 `uv.lock`에 고정했다.

## 운영자가 직접 수행하는 구독 계정 검증

`account_smoke`는 사용자가 요청한 개인 구독 계정 검증을 위한 명시적 진입점이다. 기본 `bravo-agent analyze`와 API 키 제공자는 그대로다. 공식 Codex CLI 0.157.0의 장치 로그인을 사용하며, 다른 앱에 개인 계정 접근을 제공하는 호스팅 서비스나 무인 운영 인증으로 사용하지 않는다. [공식 인증 문서](https://learn.chatgpt.com/docs/auth#login-on-headless-devices), [app-server 인증 범위](https://learn.chatgpt.com/docs/app-server#auth-endpoints)

```sh
uv run python -m bravo_agent.account_smoke \
  --codex /absolute/path/to/verified/codex \
  --bundle examples/failure.json \
  --manifest examples/snapshot-manifest.json \
  --snapshot examples/source \
  --model "$MODEL_ID" \
  --output /private/path/new-account-result.json
```

설치자는 공식 릴리스의 플랫폼별 SHA-256을 확인한 CLI 바이너리를 제공한다. 실행은 별도 비루트 사용자와 읽기 전용 시스템 파일, 실행별 임시 쓰기 공간, CPU·메모리·시간 제한을 적용한 신뢰된 호스트에서만 한다. 이 명령 자체가 호스트 격리를 구성하지는 않는다.

- CLI의 인증 저장소는 `ephemeral`이며 같은 프로세스의 메모리에만 인증을 유지한다. 개발자 인증 캐시·API 키·AWS 자격·사용자 설정을 복사하지 않는다. 인증 파일이 생성되면 실패한다. 장치 로그인 대기는 최대 10분이고 인증 뒤 제안 실행은 최대 180초다.
- 빈 실행 디렉터리, 환경 접근 없음, `read-only`·승인 거부를 설정한다. shell·apps·plugins·hooks·멀티에이전트·브라우저·이미지·외부 MCP를 제공하지 않는다. 모델에는 `repo_read`, `plan_propose`만 등록하고 기존 `Tools` 정책으로 다시 검사한다. 예상 밖 내장 작업이나 승인 요청은 실행을 종료한다.
- 모델을 명시하고 자동 대체를 허용하지 않는다. 도구 호출 64회, 보고된 누적 사용량 32,000토큰에서 중단한다. CLI 내부 요청·재시도 횟수는 Responses 제공자의 8회 제한과 같다고 주장하지 않는다. 오류 알림이 오면 중단하고 자동으로 새 시도를 만들지 않는다.
- 계정 인증 성공, 모델 응답 성공, 정책을 통과한 제안서를 별도로 확인한다. `usageLimitExceeded` 등은 원문·계정 정보 없이 허용된 오류 분류만 기록하고 `NEEDS_HUMAN`으로 종료한다. 이 결과는 T15 검증·승인·배포 성공이 아니다.

실측 결과와 실행 소스 결속은 [클라우드 계정 검증 기록](../../docs/evidence/t11-account-cloud-2026-11-03/README.md)을 따른다.
