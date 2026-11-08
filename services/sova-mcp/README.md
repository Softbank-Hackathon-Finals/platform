# SOVA MCP · T21

SOVA를 에이전트로 처음 만나는 사람을 위한 안내형 MCP 서버다. 설계는 Notion
"SOVA API · MCP 설계 명세 (초안 v0.1)", 결정은 [ADR-0040](../../docs/adr/0040-sova-mcp-surface.md).
T10의 같은 저장소(`PlanStore`)와 `Registry`를 쓰며 새 상태 머신·게이트·실행기를 만들지 않는다.

## 연결

```sh
# 증명: AWS SSO 프로필로 audience=sova-mcp STS 서명 증명(60초)을 만들어 Authorization: Bearer 로 보낸다.
claude mcp add --transport http sova "http://sova-mcp.<tailnet>.ts.net:443/mcp" --header "Authorization: Bearer <proof>"
```

SOVA Console 데스크톱 앱(`services/console/desktop`)은 요청마다 증명을 새로 붙여 기본 탑재로 연결한다.
연결 옵션: `?plan=<id>`(한 계획으로 범위 고정), `?read_only=false`(등록·계획 생성 도구 노출, 기본 true).

## 표면

| 묶음 | 도구 | 성격 |
|---|---|---|
| 맥락 | `sova_get_context` · `sova_next_steps` · `sova_list_plans` · `sova_request_approval` | 읽기. 승인은 콘솔 링크만 |
| 등록·계획 | `sova_register_environment` · `sova_register_workload` · `sova_create_plan` | 미리보기 → 같은 입력 + `confirmation` |
| 관측 | `sova_get_plan` · `sova_get_progress` · `sova_compare` · `sova_get_execution` · `sova_get_observability` · `sova_get_security` | 읽기. 새 명령 없음 |

리소스 `sova://guide/flow`(8단계) · `sova://glossary` · `sova://plans/{id}` · `sova://plans/{id}/next-actions`,
프롬프트 `sova-onboarding` · `sova-migrate` · `sova-status` · `sova-rehearsal-review` · `sova-failback`.
모든 도구는 `{status, summary, data, next_actions, poll_after_ms, cursor, console_url, confirmation, observed_at, source}`를 돌려준다.
`sova_get_context`는 등록 화면의 선택 목록도 준다: `connections`(이 테넌트의 연결 참조), `releases`(운영자가 승인한 저장소·앱·IaC 경로). 서버는 등록 때 각 값을 다시 확인한다.
승인·실행·복귀·reconcile·SAFE_HOLD·정리·리허설 시작은 도구가 없다(사람이 콘솔에서).

콘솔용 진행 스트림 `GET /v1/plans/{id}/events`는 T10 SSE 계약(`id`·`event`·`data`, `Last-Event-ID` 재개, 409 `resync_required`)을
같은 증명·테넌트·`?plan=` 범위로 연다. 읽기마다 권한을 다시 확인하고 증명 만료(≤60초) 시 스트림을 닫는다. 콘솔은 새 증명과
마지막 `id`로 다시 연결한다.

계획을 읽는 도구·리소스·스트림은 테넌트에 더해 T10 계획별 `read` 권한(`OperatorAccess`, 관리자의 `set_permissions`)을
읽을 때마다 확인한다(운영자 API·콘솔과 같은 기준). 같은 테넌트라도 권한이 없으면 목록에 계획이 나오지 않고 `forbidden`을 받는다.
권한을 회수하면 열린 스트림은 다음 읽기에서 닫힌다. MCP로 만든 계획도 관리자가 `read`를 주기 전에는 만든 사람이 읽지 못한다.

## 실행·검증

```sh
uv sync && uv run pytest -q && uvx ruff check .
docker build -f services/sova-mcp/Dockerfile .   # 저장소 루트에서
```

배포: `publish-sova-mcp-image`(develop 커밋) → `infra/charts/sova-mcp`에 digest. 운영 입력
(`sova-mcp-config` Secret의 `config.json`: `resource_url`, `allowed_hosts`, `console_url`, `identities`,
`environments`, `observability`, `security`)은 저장소 밖에서 만든다. 등록 설정 ConfigMap·수집기 Secret은
현재 `t10-registration`이 쓰는 이름을 읽기 전용으로 참조하며 그쪽을 바꾸지 않는다.
