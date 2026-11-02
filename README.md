# platform

가동 중인 서비스를 AWS에서 온프레미스로 멈추지 않고 옮기고, 옮긴 환경이 원본과 같은 수준으로 안전하고 정확한지 검증 게이트로 확인한 뒤에만 전환하는 클라우드 출구전략 플랫폼이다.

SoftBank Hackathon 2026 본선 · Team Bravo. 제품명은 미정이고, 레포 이름은 정해지면 바꾼다.

## 레포 구성

| 레포 | 내용 |
|---|---|
| `platform` (이 레포) | 플랫폼 코드(콘솔 · API · 상태 머신 · Cutover Executor · 에이전트 워커 · 원본 대조 게이트), 인프라(`infra/`), 설계 문서 · ADR, 작업 관리 |
| [`demo-chat`](https://github.com/Softbank-Hackathon-Finals/demo-chat) | 이전 대상 데모 앱(금융 상담 채팅) |

데모 앱을 따로 두는 이유: 플랫폼은 앱 레포를 범위를 좁힌 토큰(레포 읽기 · 브랜치 쓰기)으로 다룬다. 같은 레포에 두면 그 토큰이 플랫폼 코드에도 닿는다.

## 일하는 방법

진행 상황의 기준은 [프로젝트 보드](https://github.com/orgs/Softbank-Hackathon-Finals/projects/2)다. 작업 계획은 [`docs/tasks.md`](docs/tasks.md), 설계는 [`docs/design.html`](docs/design.html), 결정 기록은 [`docs/adr/`](docs/adr/)에 있다.

### 처음 한 번

1. `gh auth login`으로 GitHub CLI에 로그인한다. 작업 스킬이 `gh`로 이슈를 읽는다.
2. 레포를 클론한다: `gh repo clone Softbank-Hackathon-Finals/platform` (데모 앱 작업이면 `demo-chat`도)
3. 레포 폴더에서 Claude Code(`claude`)나 Codex를 연다. `CLAUDE.md` → `AGENTS.md`의 작업 규칙을 에이전트가 읽는다.

### 작업 하나를 끝내는 순서

| 순서 | 할 일 | 명령 · 위치 |
|---|---|---|
| 1 | 내 작업 보기, 고르기 | `/todo-task` (Codex는 `$todo-task`), 또는 "T3 하자" |
| 2 | 브랜치 만들기 | `git switch -c t3-rehearsal-ns` (`main`에 직접 push는 막혀 있다) |
| 3 | 커밋 · PR | 제목 앞에 `[T3]`. 본문에 `Refs #이슈번호`, 마지막 PR이면 `Closes #이슈번호` |
| 4 | 자동 머지 | `gh pr merge --auto --squash`. CI(`secrets` · `infra` · `platform`)가 통과하면 머지된다. 리뷰 승인은 필요 없다 |
| 5 | 할 일 체크 | 머지 후 **이슈에서** 체크(클릭 또는 `python3 scripts/sync_tasks.py tick T3 2`) |

체크하면 보드가 In progress로 옮겨지고, 할 일을 모두 체크하면 이슈가 닫히며 Done이 된다.

`/todo-task`가 1~5를 순서대로 안내한다. 처음이면 자기 작업의 첫 할 일 하나로 이 흐름을 한 번 끝까지 돌려 본다.

### 무엇을 어디서 고치나

같은 작업이 이슈(보드 카드)와 `docs/tasks.md` 두 곳에 있다. 봇(`sync-tasks`)이 둘을 맞추는데, 항목마다 기준이 되는 곳이 정해져 있다. 기준이 아닌 곳에서 고치면 다음 동기화 때 기준 쪽 값으로 덮어써진다.

| 바꾸려는 것 | 고치는 곳 | 고치면 안 되는 곳 |
|---|---|---|
| **진행 상황**: 할 일 체크, 담당(Assignees), 보드 Status | 이슈 화면 또는 보드 | `tasks.md`의 `- [x]` 체크박스와 `**담당**` 칸 (봇이 이슈를 따라 고친다) |
| **계획**: 제목, 완료 기준, 할 일 문장, 우선순위, 단계, 선행, 새 작업 | `docs/tasks.md`를 고쳐 PR | 이슈 본문 · 제목 · `P0`/`stage:`/`area:` 라벨 (보드에서 연 카드 포함) |

- 댓글, 보드의 Stage · Size 필드, 그 밖의 라벨은 자유롭게 쓴다.
- 완료는 Status를 Done으로 옮기지 말고 할 일을 모두 체크해서 한다(체크가 남아 있으면 봇이 되돌린다).
- 작업 번호(T번호)는 바꾸지 않는다. 새 작업은 다음 빈 번호로 추가한다.

### 하지 않는 것

- CI 실패를 워크플로를 고쳐서 넘기지 않는다. 검사 예외는 `.gitleaks.toml` · `.trivyignore`에 사유 주석과 함께 추가한다.
- 시크릿 값을 코드 · 로그 · PR 본문에 남기지 않는다.

### 막히면

| 증상 | 해결 |
|---|---|
| `secrets` 검사가 PR 제목 형식으로 실패 | `gh pr edit --title "[T3] …"`로 고친 뒤 `gh pr close <번호> && gh pr reopen <번호>`(제목 수정만으로는 CI가 다시 돌지 않는다) |
| `[sync]` 봇 PR이 충돌로 멈춤 | 그 PR을 닫고 Actions → `sync-tasks` → Run workflow(`pull`) |
| 내 작업이 `/todo-task`에 안 나옴 | 이슈 Assignees에 내 계정이 있는지 확인 |
