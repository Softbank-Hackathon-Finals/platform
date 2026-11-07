# T44 목적지 구축 계획: 정형 스택 → 렌더 → 계획 digest

설계 8.11의 "목적지 구축" 어댑터를 [ADR-0032](../../docs/adr/0032-infra-plan-api.md)대로 만든다. 목적지 구축 절차를 정형 단계 스택으로 고정하고, 같은 입력이면 같은 계획과 `plan_digest`가 나오게 한다. 승인은 T10 `approval_kind=infra`(#603)가, 선언형 자원의 적용은 목적지 클러스터 안의 Argo CD가 맡는다.

**상태: 스택 정의와 계획 엔진(오프라인)까지. 시험 21개 통과. 실제 chart 4개(공식 저장소에서 받은 아카이브, 고정 SHA-256 일치)와 `helm template`으로 `target-onprem` 계획을 두 번 렌더해 결과와 digest가 같음을 로컬에서 확인했다(약 1.8초). 클러스터 dry-run · 설정 저장소 커밋 · 적용 실행기 · HTTP API · T11 도구는 다음 PR이다.**

| 파일 | 역할 |
|---|---|
| `schemas/stack.schema.json` | 스택 v1. 단계 종류 8개(`probe` · `operator_action` · `helm_release` · `gitops_publish` · `gitops_sync` · `render_apply` · `secret_generate` · `wait`)와 종류별 필드. 셸 명령 필드는 없다 |
| `stack.py` | 스택 검사(스키마, 중복 · 없는 의존 · 순환, publish 없는 sync, 선언하지 않은 파라미터), 병렬 단계(level) 계산, 계획 요청 JSON Schema 생성 |
| `plan.py` | 저장소 렌더러를 고정 인자로 실행하고, chart는 아카이브 해시를 확인한 뒤 `helm template` → 후처리(`finalize-bao` 등)를 거친다. 렌더 결과를 자원 종류로 골라 나눌 수 있다. 단계 digest, 설정 저장소 트리 digest, `plan_digest` |
| `cli.py` | `bravo-infra-plan check / request-schema / plan` |
| [`infra/stacks/target-onprem/stack.json`](../../infra/stacks/target-onprem/stack.json) | 첫 스택. T30 · T9 README 절차를 33단계 15레벨로 옮겼다. 렌더러 코드는 바꾸지 않았다 |
| [`infra/stacks/target-onprem/argocd/`](../../infra/stacks/target-onprem/argocd) | 목적지 Argo CD Core chart(argo-cd 10.14.0, UI · Dex · admin · exec 없음). 플랫폼 chart에는 플랫폼 전용 자원이 항상 렌더되므로 따로 둔다 |

## 계획이 담는 것

`plan_digest` = 정규화한 계획 요청 + 스택 digest + `stack_revision`(Git SHA) + 대상 클러스터 · 노드 UID + 설정 저장소 기준 커밋 + 단계별 digest. 하나라도 바뀌면 digest가 바뀐다(시험으로 확인).

- `gitops_publish`: 설정 저장소 `<path>/<name>.json`에 올릴 매니페스트. 트리 digest에 들어간다.
- `render_apply`: 일회성 자원(마이그레이션 Job). 설정 저장소에 두지 않는다.
- `helm_release`: chart 디렉터리(`Chart.lock` 포함, 내려받은 `charts/` 제외)와 values의 digest.
- `secret_generate`: Secret 이름 · 타입 · 키 이름과 생성 방식(`literal` · `random` · `builtin`)만. 값은 실행기가 메모리에서 만든다.
- 그 밖의 단계: 단계 정의 자체의 digest.

## 사용

```sh
uv run bravo-infra-plan check ../../infra/stacks/target-onprem/stack.json
uv run bravo-infra-plan request-schema ../../infra/stacks/target-onprem/stack.json
uv run bravo-infra-plan plan ../../infra/stacks/target-onprem/stack.json \
  --request request.json --repo-root ../.. --charts-dir <chart 아카이브 폴더> \
  --stack-revision <Git SHA> --cluster-uid <UID> --node-uid <UID> --base-commit <Git SHA> \
  --out <새 출력 폴더>
```

chart 아카이브는 스택의 `charts`에 적힌 파일 이름으로 둔다. 해시가 다르면 `chart_hash_mismatch`로 거부한다. `plan`은 클러스터와 Git 원격에 접속하지 않으며, 대상 UID와 기준 커밋은 호출자가 넘긴다(다음 PR의 API는 직접 읽는다).

## 다음 PR에서 할 것

- 적용 실행기: T10 승인 소비(#603), digest · 기준 커밋 재확인, 설정 저장소 커밋, 목적지 Argo CD Application 생성(줄기별, 자동 prune 끔)과 revision 확인, 병렬 실행 · `SKIPPED` · `HOLD`, T24 기록.
- ESO chart의 `external-secrets-webhook` Secret은 빈 상태로 렌더되고 실행 중에 컨트롤러가 채운다. Application에 이 Secret과 CRD `caBundle`의 `ignoreDifferences`를 둔다.
- `probe` · `operator_action` 확인 조건(`target-identity`, `host-paths` 등)의 실제 구현.
- HTTP API와 T11 `infra.plan` 도구, 새 로컬 VM 실측.
