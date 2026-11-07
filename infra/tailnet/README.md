# tailnet 정책 조각 (T44)

`routes-policy.json`(기본 tailnet)과 `routes-policy.workload.json`(워크로드 tailnet)은 `infra/iac/environments/*.json`의 `tailnet-egress` 선언에서 생성한 **조각**이다. tailnet마다 정책이 따로 있으므로 조각도 나눈다. 손으로 고치지 않는다.

```sh
cd services/iac
uv run bravo-iac tailnet-policy ../../infra/iac/environments/*.json > ../../infra/tailnet/routes-policy.json
uv run bravo-iac tailnet-policy --tailnet workload ../../infra/iac/environments/*.json > ../../infra/tailnet/routes-policy.workload.json
uv run bravo-iac tailnet-check --policy <현재 tailnet 정책 JSON> ../../infra/iac/environments/*.json
```

- 담는 것: 그룹 태그 소유자, 경로마다 grant 하나(그룹 태그 → 목적지 태그:포트), 정책 `tests`(경로 허용, 22 거부).
- tailnet 관리자는 이 조각을 현재 정책에 합치기 전에 Tailscale validate를 돌리고, 현재 정책과의 차이를 확인한다.
- 시험(`services/iac/tests/test_tailnet.py`)이 이 파일과 원하는 상태의 렌더가 같은지 고정한다.
- 조각은 `compose_policy.py`가 `base-policy.json` · `identities.json`과 합쳐 정책 전체를 만든다. 적용은 [`infra/envs/tailnet`](../envs/tailnet/README.md)(T48, ADR-0035).
