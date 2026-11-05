# 에이전트 작업 안내

이 레포는 클라우드 출구전략 플랫폼(SoftBank Hackathon 2026 본선 · Team Bravo)의 플랫폼 코드, 인프라, 설계 문서, 작업 관리를 담는다. 이전 대상 데모 앱은 `demo-chat` 레포에 있다.

## 작업 전에

1. `docs/tasks.md`를 먼저 읽는다. 팀 작업 계획이고, 맨 위 **작업 규칙**을 따른다. 진행 상황의 기준은 [프로젝트 보드](https://github.com/orgs/Softbank-Hackathon-Finals/projects/2)(이슈)다.
2. 사용자의 작업은 이슈 Assignees로 찾고, **선행** 작업이 끝났는지 확인한다. `통합 시점` 작업은 시작 조건이 아니다(모의 환경으로 먼저 시작하고 완료 확인만 그 뒤에 한다).
3. `develop`에서 `feature/T번호-설명` 브랜치를 만들고 `develop`으로 PR을 올린다. `main`은 release/hotfix로 반영한다. 승인·CODEOWNERS·tasks.md 전용 예외·봇 PR 병합은 [개발 규약](docs/development.md#브랜치와-병합)과 현재 GitHub 규칙을 따른다. 보호 규칙을 우회하지 않는다.
   다른 담당자의 열린 태스크를 바꾸기 전에는 [담당 범위 규약](docs/development.md#담당-태스크를-넘는-변경)에 따라 범위를 합의한다. 로컬 세션 배정이나 사후 승인은 이를 대신하지 않는다.
4. 커밋 메시지와 PR 제목 앞에 `[T번호]`. 끝낸 할 일은 PR 머지 후 **이슈에서** 체크한다(`python3 scripts/sync_tasks.py tick T번호 순번`). tasks.md 체크박스와 담당은 봇이 고친다.

리뷰 요청 대상은 [파트별 리뷰 기준](docs/review-routing.md)에 따라 작업 번호의 주·부 참여자를 모두 포함하고 작성자는 제외한다. 초기 허들 댓글만으로 후속 공식 배정을 되돌리지 않는다.

이 순서는 `todo-task` 스킬에 정리돼 있다 (Claude Code `/todo-task`, Codex `$todo-task`).

## Slack 메시지

Slack 메시지를 작성하거나 전송할 때 [Slack 메시지 작성 기준](docs/slack-writing.md)을 읽고 따른다.

## 설계 · 결정

- 설계 문서: `docs/design.html` (용어는 부록). 원본은 이 파일이다(노션은 링크만 둔다). 진행 상황은 적지 않는다.
- 설계 문서의 의미 있는 변경(아키텍처 · 시나리오 · 범위 · 요구사항)은 버전을 올린다: `scripts/design-bump.sh <새 버전>`으로 이전 버전을 `docs/design-history/`에 보관하고 시작한다. 오타 · 실측값 같은 작은 수정은 그대로 고친다. 애매하면 사용자에게 묻는다. 기준과 순서는 `docs/design-history/README.md`. 버전을 올리지 않고 설계 문서를 바꾸면 CI(`design-doc`)가 경고를 남긴다.
- 생산자·소비자 경계를 바꾸기 전에 [계약 목차](docs/contracts/README.md)에서 기존 원문과 소비자를 확인한다. 같은 계약을 새 문서·판정기로 복제하지 않는다.
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
- 검사 예외는 `.gitleaks.toml` · `.trivyignore.yaml`에 사유와 적용 경로를 명시해서만 추가한다. 임시 예외는 만료일도 둔다.

## 검증

PR마다 CI(`ci.yml`)가 `secrets` · `infra` · `platform`을 검사한다. CI 실패를 워크플로를 고쳐서 넘기지 않는다. 로컬에서는 바꾼 부분에 맞춰 돌린다.

- Terraform: `terraform fmt -check -recursive`, 모듈 폴더마다 `terraform init -backend=false && terraform validate`
- Python(`pyproject.toml`): `uv sync`, `uvx ruff check .`, `uv run pytest`
- Node(`package.json`): `pnpm lint`, `pnpm test`, `pnpm build`
- Go(`go.mod`): `go vet ./...`, `go test ./...`
- 워크플로: `actionlint`

CI 실행 범위와 플랫폼 S2 실패 분석의 경계는 [CI 안내](docs/ci.md)를 따른다. 필수 결과는 항상 보고하며, 변경 범위가 불명확하면 전체 검사한다.

여러 PR의 리뷰 확인·수정·해소는 [리뷰 플레이북](docs/review-playbook.md)과 `.agents/skills/pr-review/SKILL.md`를 따른다. `python3 scripts/review_packet.py <PR번호...> --output-dir <개인 출력 경로>`로 현재 SHA의 근거를 수집하고 판단 기록을 남긴다. 수집 성공이나 스레드 해소는 팀원 승인을 대신하지 않는다.
# 로컬 VM 실행 아키텍처

새 목적지 VM은 [정본 생성기](infra/envs/onprem/README.md)로 호스트와 같은 아키텍처·하드웨어 가속을 사용한다. `.collab` 등의 과거 실행 스크립트를 새 VM 생성기로 재사용하지 않는다. 이미지 아키텍처가 맞지 않으면 네이티브 이미지를 준비하고, 호환성 시험만 목적·사유를 지정해 에뮬레이션을 허용한다. 성능 실측 전에 `local-vm.sh measurement`의 현재 프로세스 검증을 실행 근거에 결속한다. 기존 VM의 실측·데이터를 보존하며 이 규칙만으로 재시작·이관·증설하지 않는다.
