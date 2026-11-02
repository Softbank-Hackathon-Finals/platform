# 에이전트 작업 안내

이 레포는 클라우드 출구전략 플랫폼(SoftBank Hackathon 2026 본선 · Team Bravo)의 플랫폼 코드, 인프라, 설계 문서, 작업 관리를 담는다. 이전 대상 데모 앱은 `demo-chat` 레포에 있다.

## 작업 전에

1. `docs/tasks.md`를 먼저 읽는다. 팀 작업 계획이고, 맨 위 **작업 규칙**을 따른다. 진행 상황의 기준은 [프로젝트 보드](https://github.com/orgs/Softbank-Hackathon-Finals/projects/2)(이슈)다.
2. 사용자의 작업은 이슈 Assignees로 찾고, **선행** 작업이 끝났는지 확인한다. `통합 시점` 작업은 시작 조건이 아니다(모의 환경으로 먼저 시작하고 완료 확인만 그 뒤에 한다).
3. `main`은 보호돼 있다. 브랜치에서 작업하고 PR로 올린다(CI 통과 시 자동 머지, 리뷰 승인 불필요). `.github/workflows/`와 `CODEOWNERS`를 바꾸는 PR만 본인 외 팀원 1명의 승인이 필요하다.
4. 커밋 메시지와 PR 제목 앞에 `[T번호]`. 끝낸 할 일은 PR 머지 후 **이슈에서** 체크한다(`python3 scripts/sync_tasks.py tick T번호 순번`). tasks.md 체크박스와 담당은 봇이 고친다.

이 순서는 `todo-task` 스킬에 정리돼 있다 (Claude Code `/todo-task`, Codex `$todo-task`).

## 설계 · 결정

- 설계 문서: `docs/design.html` (용어는 부록). 진행 상황은 적지 않는다.
- 결정은 ADR로 남긴다: `docs/adr/NNNN-제목.md`, 형식은 `docs/adr/0000-template.md`. 실패한 실험도 ADR로 남긴다(T29).

## 지켜야 할 설계 원칙

설계 문서 1장 · 8.3의 원칙이다. 코드 리뷰와 구현에서 어기지 않는다.

- **결정적 워크플로가 흐름을 쥔다.** AI(에이전트 워커)는 정해진 지점에서 JSON Schema로 검증되는 제안서만 낸다. 실행과 판정은 코드가 한다.
- **판정 · 실행 모듈에 LLM을 쓰지 않는다.** 원본 대조 게이트와 Cutover Executor는 LLM 클라이언트를 import하지 않도록 모듈 경계를 나눈다.
- **에이전트에게 실행 권한을 주지 않는다.** 원본은 읽기 전용, 쓰기는 리허설 네임스페이스만. 파괴적 명령(`rm -rf`, `DROP`, `TRUNCATE`, `terraform destroy`, `kubectl delete namespace`, 버킷 삭제 등)은 프롬프트가 아니라 도구 계층에서 막는다.
- **불명확하면 쓰기를 열지 않는다.** 어느 순간에도 업무 쓰기를 받는 DB는 최대 하나다. 상태를 확인할 수 없으면 SAFE_HOLD로 멈춘다.
- **동등성은 시험 결과로 판단한다.** 제품 이름이 아니라, 원본에서 거부되던 접근이 대상에서도 실제로 거부되는지 시험한다.

## 보안 규칙

- 장기 액세스 키를 공유하거나 커밋하지 않는다. 사람은 각자 계정, CI는 OIDC로 권한을 받는다.
- 시크릿 값을 로그 · 파일 · 커밋 · PR 본문에 남기지 않는다. 모델 키는 플랫폼에만 둔다.
- 모든 클라우드 자원에 `component` 태그를 단다(비용 정산, 데모 후 `terraform destroy`).
- 검사 예외는 `.gitleaks.toml` · `.trivyignore`에 사유 주석과 함께만 추가한다.

## 검증

PR마다 CI(`ci.yml`)가 `secrets` · `infra` · `platform`을 검사한다. CI 실패를 워크플로를 고쳐서 넘기지 않는다. 로컬에서는 바꾼 부분에 맞춰 돌린다.

- Terraform: `terraform fmt -check -recursive`, 모듈 폴더마다 `terraform init -backend=false && terraform validate`
- Python(`pyproject.toml`): `uv sync`, `uvx ruff check .`, `uv run pytest`
- Node(`package.json`): `pnpm lint`, `pnpm test`, `pnpm build`
- Go(`go.mod`): `go vet ./...`, `go test ./...`
- 워크플로: `actionlint`
