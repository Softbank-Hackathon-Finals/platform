# 목적지 IaC (T44)

목적지 환경의 구성을 **모듈 + 원하는 상태(Git)**로 관리한다. 플랫폼은 모듈 목록과 계획(plan)을 API로 주고,
에이전트는 계획 단계에서 그 API로 변경을 **제안**한다. 적용은 GitOps다: 사람이 검토·병합한 원하는 상태만
적용 대상이 된다. 에이전트는 계획만 하고 적용하지 않는다(설계 원칙: 제안은 AI, 실행은 코드).

| 파일 | 뜻 |
|---|---|
| `environments/<환경>.json` | 그 환경의 원하는 상태. `{schema_version, environment, modules: [{id, version, params}]}` |
| `services/iac` (`bravo_iac`) | 모듈 카탈로그, 상태 검사, 결정적 plan, CLI(`bravo-iac`), 읽기·제안 API |

## 모듈

| id · version | 소유 자원 | 파라미터 |
|---|---|---|
| `target-object-exposure` v1 | Secret `target-dev/target-seaweedfs-auth`의 `s3.json` 계정·권한 형태(비밀 값 제외), 바뀌면 `target-seaweedfs` 재시작 | `state`: `closed` \| `open-demo`, `bucket`, `demo_run_id`(open-demo일 때만) |

`target-object-exposure`는 T9 렌더러(`infra/envs/target-workloads/object_exposure.py`)와 같은 계정·권한을 낸다.
같은지는 시험이 고정한다. 계정 키는 Git에도 plan에도 없다. 적용하는 쪽이 적용 시점에 넣는다(`secret_inputs`).

`environments/target-dev.json`은 지금 **`open-demo`**다. 데모 1번(IAM-01.1: 인증 없이 첨부를 읽을 수 있는가)의
일부러 심은 결함이며 `demo_run_id`로 그 실행을 밝힌다. 운영 기본값은 `closed`다.

## 흐름

```sh
cd services/iac
uv run bravo-iac modules
uv run bravo-iac plan --current ../../infra/iac/environments/target-dev.json --proposed proposed.json --origin agent > plan.json
uv run bravo-iac stage --plan plan.json --proposed proposed.json --target ../../infra/iac/environments/target-dev.json
# → 바뀐 target-dev.json으로 PR을 연다. PR 본문에 plan_digest와 effects를 붙인다. 검토·병합이 승인이다
```

- plan은 같은 입력에서 항상 같은 `plan_digest`를 낸다. `authorizes_apply`는 항상 `false`다.
- **에이전트(`--origin agent`)는 노출을 넓힐 수 없고(`IAC_WIDENING_REFUSED`), 환경의 모듈을 더하거나 뺄 수 없다**
  (`IAC_MODULE_SET_CHANGE_REFUSED`). 운영자는 할 수 있고, plan에 누가 요청했는지 남는다.
- `stage`는 대상 파일이 plan의 `base_digest`일 때만 쓴다. 그사이 Git이 바뀌었으면 `IAC_BASE_CHANGED`로 멈춘다.
- API(`bravo_iac.api.create_app(<environments 폴더>)`): `GET /v1/iac/modules`, `GET /v1/iac/environments/{id}`,
  `POST /v1/iac/plan`. 커밋된 파일만 읽고 Git·클러스터에 쓰지 않는다. 호출자 인증은 앞단 호스트가 한다.

## 아직 아닌 것

- 병합 뒤 자동 적용기(Argo CD 또는 T31 apply-host가 원하는 상태를 읽어 적용·검증). 지금은 T9 `object_exposure.py plan`의
  운영 순서로 적용하고, GATE가 IAM-01.1을 다시 대조한다.
- 에이전트 도구 `infra.plan`(T11)과 플랫폼 API 연결(T10). 이 패키지는 둘 다 네트워크 없이 부를 수 있게 순수 함수로 둔다.
- 모듈 추가(목적지 앱 Deployment·NetworkPolicy·OpenBao 정책 등).
