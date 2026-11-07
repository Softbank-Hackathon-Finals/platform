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

| `tailnet-egress` v1 | 클러스터의 Tailscale egress 경로: 그룹마다 egress ProxyGroup(전용 태그), tailnet 호스트로 가는 ExternalName Service, 그 그룹 proxy Pod에 묶은 NetworkPolicy | `owner_tag`, `groups[]`: `name`, `tag`, `proxy_class`?, `clients[]`(namespace·labels), `routes[]`(service·namespace·destination·destination_tag·port·port_name) |

`target-object-exposure`는 T9 렌더러(`infra/envs/target-workloads/object_exposure.py`)와 같은 계정·권한을 낸다.
같은지는 시험이 고정한다. 계정 키는 Git에도 plan에도 없다. 적용하는 쪽이 적용 시점에 넣는다(`secret_inputs`).

`environments/target-dev.json`은 지금 **`open-demo`**다. 데모 1번(IAM-01.1: 인증 없이 첨부를 읽을 수 있는가)의
일부러 심은 결함이며 `demo_run_id`로 그 실행을 밝힌다. 운영 기본값은 `closed`다.

## 네트워크 경로를 미리 깐다 (`tailnet-egress`)

지금까지는 경로마다 렌더러가 있고(`rehearsal-access`, `executor-network`, `collector-network`), 적용 뒤
Tailscale operator가 매긴 proxy 포트를 **관찰해서** NetworkPolicy에 넣었다. 그래서 작업할 때마다 들어가서
포트·주소를 찾아야 했다. 이 모듈은 경로를 Git에 선언해 처음부터 깐다.

- NetworkPolicy는 그 그룹의 proxy Pod(선택자)와 TCP 10000–10999(`endPort`)만 연다. Tailscale 1.104 operator가
  egress 경로를 이 범위의 포트로 매기므로 적용 뒤 관찰할 것이 없다. 그룹은 용도별 전용이라 열린 범위는 그 용도의
  경로뿐이다. **목적지·포트의 실제 허용은 tailnet grant가 정한다.** 기존 rehearsal-access의 "할당된 포트만 연다"보다
  클러스터 쪽은 넓어지는 트레이드오프다(ADR-0032).
- tailnet 정책 조각 `infra/tailnet/routes-policy.json`은 원하는 상태에서 결정적으로 생성한다(`bravo-iac tailnet-policy`).
  그룹 태그의 소유자, 경로마다 grant 하나, Tailscale 정책 `tests`(경로 허용, 같은 목적지 22 거부)를 담는다.
  `tests`가 있어서 Tailscale이 이 불변식을 깨는 정책을 거부한다.
- tailnet 접미사(`<tailnet>.ts.net`)는 Git에 없다. Service에는 `<호스트>.{tailnet}`으로 두고 적용기가 채운다.
- 에이전트는 경로를 더할 수 없고(도달 범위가 늘면 `IAC_WIDENING_REFUSED`) 뺄 수만 있다.

**실검증은 Tailscale로**: `bravo-iac tailnet-verify infra/iac/environments/*.json`이 이 기기의 `tailscale status --json`으로
각 목적지·proxy 그룹을 판정한다(OK / OFFLINE / TAG_MISMATCH / NOT_VISIBLE). NOT_VISIBLE은 이 기기의 grant로 보이지
않는다는 뜻이며 경로 실패가 아니다. 전체 확인은 tailnet 장치 목록이 보이는 운영 기기에서 한다.
`bravo-iac tailnet-check --policy <현재 tailnet 정책 JSON>`은 현재 정책에 빠진 태그 소유·grant·tests를 코드로 낸다.

2026-11-07 이 맥에서 실행한 결과: 목적지 API 장치 `bravo-target-operator`의 실제 태그가 `tag:bravo-target-operator`라
선언(`tag:bravo-target-api`, 저장소 조각과 같음)과 **TAG_MISMATCH**였다. 플랫폼 → 목적지 API grant가 실제 장치와 맞지
않는다는 뜻이다. API proxy에 전용 태그를 줄지 grant 대상을 바꿀지는 목적지 담당이 정한다. 나머지 11개는 NOT_VISIBLE.

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
- 모듈 추가(목적지 앱 Deployment·NetworkPolicy·OpenBao 정책 등), 목적지 쪽 tailnet 노출(API proxy·Pooler·DB의 태그).
- tailnet 정책의 GitOps 적용(PR에서 Tailscale validate, 머지 때 apply)과 기존 경로 렌더러의 이 모듈로 이전.
