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
승인·실행·복귀·reconcile·SAFE_HOLD·정리·리허설 시작은 도구가 없다(사람이 콘솔에서).

## 실행·검증

```sh
uv sync && uv run pytest -q && uvx ruff check .
docker build -f services/sova-mcp/Dockerfile .   # 저장소 루트에서
```

배포: `publish-sova-mcp-image`(develop 커밋) → `infra/charts/sova-mcp`에 digest. 운영 입력
(`sova-mcp-config` Secret의 `config.json`: `resource_url`, `allowed_hosts`, `console_url`, `identities`,
`environments`, `observability`, `security`)은 저장소 밖에서 만든다. 등록 설정 ConfigMap·수집기 Secret은
현재 `t10-registration`이 쓰는 이름을 읽기 전용으로 참조하며 그쪽을 바꾸지 않는다.
