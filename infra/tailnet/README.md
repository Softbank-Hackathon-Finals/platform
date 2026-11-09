# tailnet 정책 조각 (T44)

`routes-policy.json`(기본 tailnet)과 `routes-policy.workload.json`(워크로드 tailnet)은 `infra/iac/environments/*.json`의 `tailnet-egress` 선언에서 생성한 **조각**이다. tailnet마다 정책이 따로 있으므로 조각도 나눈다. 손으로 고치지 않는다.

```sh
cd services/iac
uv run bravo-iac tailnet-policy ../../infra/iac/environments/*.json > ../../infra/tailnet/routes-policy.json
uv run bravo-iac tailnet-policy --tailnet workload ../../infra/iac/environments/*.json > ../../infra/tailnet/routes-policy.workload.json
uv run bravo-iac tailnet-check --policy <현재 tailnet 정책 JSON> ../../infra/iac/environments/*.json
```

- 담는 것: 그룹 태그 소유자, 경로마다 grant 하나(그룹 태그 → 목적지 태그:포트), 정책 `tests`(경로 허용, 22 거부). 피어 릴레이(`tailnet-peer-relay`)는 사용 태그마다 `ip` 없는 capability grant 하나이고 시험이 없다(Tailscale 정책 `tests`는 capability를 검사하지 않는다).
- 시험(`services/iac/tests/test_tailnet.py`)이 이 파일과 원하는 상태의 렌더가 같은지 고정한다.
- 환경별 operator 연합 신원은 [`infra/envs/tailnet`](../envs/tailnet/README.md)(T48, ADR-0035)가 만든다. 정책은 쓰지 않는다.

## 적용 (Tailscale GitOps)

관리 콘솔에 조각을 손으로 붙이지 않는다. [`tailnet-policy` 워크플로](../../.github/workflows/tailnet-policy.yml)가 tailnet마다 같은 일을 한다.

| 언제 | 하는 일 | 쓰는 권한 |
|---|---|---|
| develop으로 가는 PR | 현재 정책을 읽어 조각을 합치고 `tailnet-check`와 Tailscale validate로 검사한다. 정책에 쓰지 않는다 | 읽기(`policy_file:read`) |
| develop 병합 뒤(환경 승인 뒤) | 같은 합치기와 validate를 통과하면 읽을 때의 ETag로(`If-Match`) 쓰고, 다시 읽어 더하기·빼기가 다 반영됐는지 대조한다 | 쓰기(`policy_file`) |

**저장소에는 조각만 둔다.** 실제 정책에는 그룹 구성원 이메일이 있어서 전체 정책 파일을 공개 저장소에 두지 않는다. 그래서 합치기는 저장소가 넣은 항목만 더하고 뺀다(`services/iac/src/bravo_iac/tailnet_sync.py`).

- 조각이 가진 네 부분(`tagOwners`, `autoApprovers`, `grants`, `tests`)만 다룬다. 그룹·ssh·손으로 넣은 grant 등 나머지는 그대로 둔다.
- 없는 태그 소유자·자동 승인·grant·test를 더한다. 이미 같은 허용을 주는 grant나 같은 판정을 하는 test가 있으면 더하지 않는다.
- 같은 태그의 소유자가 다르면 덮어쓰지 않고 `IAC_POLICY_CONFLICT`로 멈춘다. 사람이 정한다.
- prune: 기준 커밋의 조각에는 있었는데 이번 조각에서 빠진 태그 소유자·grant·test를 지운다. 기준 커밋은 **마지막으로 성공한 develop run**의 커밋이다(PR 계획도 같다). 취소·거부·승인 대기인 run은 기준을 옮기지 않으므로 그 run이 뺀 항목은 다음 run이 지운다. 기준을 읽지 못하면 지우지 않고 넘어가는 대신 job이 실패한다.
- **조각과 정확히 같은 항목은 누가 넣었든 저장소 소유로 본다.** 먼저 손으로 넣은 grant라도 Git이 같은 경로를 선언했다가 빼면 지워진다. 사람이 바꾼 항목(넓힌 grant, 소유자를 바꾼 태그)은 남고, 그것이 뺀 허용을 아직 주거나 뺀 시험을 아직 주장하면 결과의 `kept`에 나온다.
- develop 적용은 develop head와 이 run의 커밋이 워크플로가 읽는 경로(`infra/iac/environments`, `infra/tailnet`, `services/iac/src`, 이 워크플로)에서 같을 때만 쓴다. 다르면 `IAC_STALE_RUN`으로 실패한다(옛 run의 Re-run이 그 사이 되돌린 항목을 지우지 않게). 이 경로와 무관한 병합은 상관없다. 다시 적용하려면 옛 run을 Re-run하지 말고 develop에서 `workflow_dispatch`로 새로 돌린다.
- 자동 승인(`autoApprovers`)은 조각에 없어 지우지 않는다.
- 기준 조각은 지금 코드로 다시 만든다. `fragment()`의 출력 형식을 바꾸는 PR에서는 옛 형식 항목이 정확히 같지 않아 지워지지 않는다. 그 PR의 계획 결과에서 `kept`를 보고 옛 항목을 정리한다(validate가 막으면 run이 실패한다).
- 정책에 HuJSON 주석이 있으면 JSON으로 쓰면서 주석이 사라지므로 `IAC_POLICY_HAS_COMMENTS`로 멈춘다.
- 읽은 뒤 누가 정책을 바꿨으면 `IAC_POLICY_CHANGED`로 멈춘다(덮어쓰지 않음). 워크플로를 다시 돌리면 새 정책 위에서 합친다.
- 쓰기 응답을 잃으면 다시 보내지 않는다. 다시 읽어 판정하고, 확인하지 못하면 `UNKNOWN`(`IAC_APPLY_UNCONFIRMED`)이다.
- 출력과 job 요약에는 고정 코드와 조각 항목만 나온다. 정책 본문·구성원은 나오지 않는다.

손으로 미리 보려면(정책 JSON은 관리 콘솔 JSON editor에서 받은 개인 파일):

```sh
cd services/iac
uv run bravo-iac tailnet-merge --policy <현재 정책 JSON> [--tailnet workload] [--out <합친 정책>] ../../infra/iac/environments/*.json
```

### tailnet 관리자가 한 번 할 일

Tailscale 자격은 GitHub에 저장하지 않는다. job의 GitHub OIDC 토큰을 Tailscale [workload identity federation](https://tailscale.com/docs/features/workload-identity-federation)으로 짧은 API 토큰과 바꾼다. tailnet마다:

1. 관리 콘솔 Trust credentials에서 GitHub 발급자(`https://token.actions.githubusercontent.com`)를 믿는 연합 신원 둘을 만든다. 이 저장소의 OIDC subject는 ID 고정 형식이다(`gh api repos/Softbank-Hackathon-Finals/platform/actions/oidc/customization/sub`의 `sub_claim_prefix`). 이름 형식(`repo:Softbank-Hackathon-Finals/platform:...`)으로 만들면 토큰 교환이 거부된다.
   - 계획용: subject `repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:environment:tailnet-policy-plan`, 범위 `policy_file:read`
   - 적용용: subject `repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:environment:tailnet-policy`, 범위 `policy_file`, 사용자 claim `ref` = `refs/heads/develop`. PR이 워크플로를 고쳐 적용 환경 이름을 써도 ref가 다르면 토큰을 받지 못한다.
2. 저장소 Actions **secret**을 넣는다. 값 자체로는 아무 권한도 없지만, 공개 저장소의 로그에 남지 않도록 secret으로 둔다. `<KEY>`는 `DEFAULT` 또는 `WORKLOAD`다.
   - `TAILNET_POLICY_NAME_<KEY>`: tailnet 조직 이름 또는 tailnet ID(콘솔 Settings → General). `-`는 쓰지 않는다. 다른 tailnet의 토큰이면 읽기부터 거부된다.
   - `TAILNET_POLICY_PLAN_CLIENT_<KEY>`, `TAILNET_POLICY_APPLY_CLIENT_<KEY>`: 위 두 신원의 client ID
3. 저장소 Environments에서 `tailnet-policy`의 배포 브랜치를 `develop`만 허용하고 필수 승인자를 둔다. develop은 승인 0이라 병합만으로 적용되면 안 된다(ADR-0032). 정책 쓰기는 고객 클러스터 cluster-admin 경계이고 prune은 지운다.

둘 다 없는 tailnet은 job 요약에 `SKIPPED`로 남고 실패하지 않는다. 이름과 client ID 중 하나만 있으면 실패한다. 계획용 신원으로 validate가 거부되면(`SKIPPED_SCOPE`) PR에서는 합치기와 `tailnet-check`까지만 보고, develop 적용은 validate PASS 없이는 쓰지 않는다.

### 긴급 수정(break-glass)

1. **적용 멈춤:** 대기 중인 develop run은 환경 승인 화면에서 거부한다. 계속 멈추려면 `gh workflow disable tailnet-policy`로 워크플로를 끈다.
2. **회수:** tailnet 관리자가 관리 콘솔에서 해당 grant를 지운다. Git에 그 경로가 남아 있으면 다음 적용이 다시 더하므로, 이어서 그 경로를 `infra/iac/environments`에서 빼는 PR을 병합한다. 그 PR의 적용에서는 이미 없는 항목이라 지울 것도 없다.
3. **신원 회수:** 적용 신원이 의심되면 관리 콘솔 Trust credentials에서 적용용 연합 신원을 Revoke한다. 다시 만들면 secret `TAILNET_POLICY_APPLY_CLIENT_<KEY>`를 바꾼다.
4. 끈 워크플로는 Git과 실제 정책이 다시 맞는지 PR 계획으로 확인한 뒤 `gh workflow enable tailnet-policy`로 켠다.

계획용 신원은 ref 제한이 없다. 저장소 쓰기 권한이 있는 사람은 어느 브랜치에서든 `policy_file:read` 토큰으로 정책 전체(구성원 이메일 포함)를 읽을 수 있다. 근본 해결은 실제 정책의 이메일을 그룹·autogroup으로 옮기는 것이다.
