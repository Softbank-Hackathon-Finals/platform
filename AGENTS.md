# 에이전트 작업 안내

이 레포는 클라우드 출구전략 플랫폼(SoftBank Hackathon 2026 본선 · Team Bravo)의 플랫폼 코드, 인프라, 설계 문서, 작업 관리를 담는다. 이전 대상 데모 앱은 `demo-chat` 레포에 있다.

## 작업 전에

1. `docs/tasks.md`를 먼저 읽는다. 팀 작업 계획이고, 맨 위 **작업 규칙**을 따른다. 진행 상황의 기준은 [프로젝트 보드](https://github.com/orgs/Softbank-Hackathon-Finals/projects/2)(이슈)다.
2. 사용자의 작업은 이슈 Assignees로 찾고, **선행** 작업이 끝났는지 확인한다. `통합 시점` 작업은 시작 조건이 아니다(모의 환경으로 먼저 시작하고 완료 확인만 그 뒤에 한다).
3. `develop`에서 `feature/T번호-설명` 브랜치를 만들고 `develop`으로 PR을 올린다(CI 통과 시 merge commit으로 자동 머지, 리뷰 승인 불필요). `main`은 release/hotfix로 반영한다. 두 브랜치 모두 보호하며, 자세한 순서는 `docs/development.md`를 따른다. `.github/workflows/`와 `CODEOWNERS`를 바꾸는 PR은 팀원에게 리뷰 요청 알림이 간다(승인은 필요 없음).
4. 커밋 메시지와 PR 제목 앞에 `[T번호]`. 끝낸 할 일은 PR 머지 후 **이슈에서** 체크한다(`python3 scripts/sync_tasks.py tick T번호 순번`). tasks.md 체크박스와 담당은 봇이 고친다.

이 순서는 `todo-task` 스킬에 정리돼 있다 (Claude Code `/todo-task`, Codex `$todo-task`).

## 설계 · 결정

- 설계 문서: `docs/design.html` (용어는 부록). 원본은 이 파일이다(노션은 링크만 둔다). 진행 상황은 적지 않는다.
- 설계 문서의 의미 있는 변경(아키텍처 · 시나리오 · 범위 · 요구사항)은 버전을 올린다: `scripts/design-bump.sh <새 버전>`으로 이전 버전을 `docs/design-history/`에 보관하고 시작한다. 오타 · 실측값 같은 작은 수정은 그대로 고친다. 애매하면 사용자에게 묻는다. 기준과 순서는 `docs/design-history/README.md`. 버전을 올리지 않고 설계 문서를 바꾸면 CI(`design-doc`)가 경고를 남긴다.
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
- 공개 문서·증빙의 개인 이메일, 홈 절대경로, 계정·운영 리소스 식별자는 가린다. 원본은 비공개로 보존하고 [공개 증빙 절차](docs/public-evidence-policy.md)를 따른다. `python3 scripts/public_evidence.py check`는 값 없이 위반 종류만 출력한다.
- 모든 클라우드 자원에 `component` 태그를 단다(비용 정산, 데모 후 `terraform destroy`).
- 검사 예외는 `.gitleaks.toml` · `.trivyignore`에 사유 주석과 함께만 추가한다.

## 검증

PR마다 CI(`ci.yml`)가 `secrets` · `infra` · `platform`을 검사한다. CI 실패를 워크플로를 고쳐서 넘기지 않는다. 로컬에서는 바꾼 부분에 맞춰 돌린다.

- Terraform: `terraform fmt -check -recursive`, 모듈 폴더마다 `terraform init -backend=false && terraform validate`
- Python(`pyproject.toml`): `uv sync`, `uvx ruff check .`, `uv run pytest`
- Node(`package.json`): `pnpm lint`, `pnpm test`, `pnpm build`
- Go(`go.mod`): `go vet ./...`, `go test ./...`
- 워크플로: `actionlint`

여러 PR의 리뷰 확인·수정·해소는 [리뷰 플레이북](docs/review-playbook.md)과 `.agents/skills/pr-review/SKILL.md`를 따른다. `python3 scripts/review_packet.py <PR번호...> --output-dir <개인 출력 경로>`로 현재 SHA의 근거를 수집하고 판단 기록을 남긴다. 수집 성공이나 스레드 해소는 팀원 승인을 대신하지 않는다.
