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

## 적용: 고객 클러스터마다 Argo CD (pull GitOps)

플랫폼 Argo CD가 여러 고객 클러스터를 밀어 넣지 않는다. **각 고객 클러스터에 Argo CD를 하나 두고**, 그 클러스터의
환경 한 곳(`infra/iac/charts/<환경>`)만 끌어와 적용한다. 고객 클러스터 수가 늘어도 플랫폼 쪽 부담·자격이 늘지 않고,
고객 클러스터의 API를 플랫폼에 열 필요가 없다.

| 경로 | 뜻 |
|---|---|
| `infra/iac/charts/<환경>` | `bravo-iac chart`로 생성한 Helm 차트(손으로 고치지 않음). 값은 `tailnet`(`<이름>.ts.net`) 하나 |
| `infra/iac/argocd` | 고객 클러스터에 설치하는 Argo CD(argo-helm 10.14.0, 플랫폼과 같은 판, Core 모드: UI·SSO·ingress 없음) + AppProject `bravo-iac` + 그 환경의 Application |

```sh
# 차트 생성(원하는 상태가 바뀌면 같은 PR에서 다시 생성한다. 시험이 일치를 고정한다)
cd services/iac && uv run bravo-iac chart ../../infra/iac/environments/target-dev.json --out ../../infra/iac/charts/target-dev
# 고객 클러스터에 한 번 설치(운영자). tailnet 접미사는 이 명령의 값으로만 들어가고 Git에 남지 않는다
helm dependency update infra/iac/argocd
helm upgrade --install bravo-iac-gitops infra/iac/argocd -n bravo-iac-gitops --create-namespace \
  --set environment=target-dev --set tailnet=<이름>.ts.net
```

이후 원하는 상태의 변경은 PR → 병합 → 그 클러스터의 Argo가 60초 안에 끌어와 동기화(자동, prune·selfHeal, 없는 namespace 생성)
한다. 비밀 값이 필요한 자원(지금은 버킷 계정 Secret)은 차트에 없고 `Chart.yaml`의 `bravo.dev/unmanaged`에 적힌다.
ProxyGroup은 그 클러스터에 Tailscale operator(CRD)가 있어야 생성된다(`SkipDryRunOnMissingResource`).

**설치 명령은 두 번 실행한다.** 첫 실행이 Argo CD와 CRD를 만들고, 두 번째 실행이 AppProject·Application을 만든다
(Application은 Argo CRD가 있을 때만 렌더된다. 한 릴리스로 함께 만들면 CRD 등록 전 검증에서 실패한다).

**로컬 실클러스터 확인(2026-11-07, kind v1.34.0, 운영 클러스터 아님)**: Tailscale 1.104.4 CRD만 설치(operator 없음) →
위 설치 명령 2회(`environment=platform`, `tailnet=example-tail.ts.net`, 기능 브랜치) → Argo가 GitHub에서 끌어와 `4bc41c87`을
Synced/Healthy로 적용했다(namespace 5, ProxyGroup 4, ExternalName Service 7: FQDN에 설치 값만 들어감, NetworkPolicy 9:
TCP 10000–10999). 이어서 에이전트 계획(`origin=agent`, 컷오버 → 목적지 Pooler 경로 제거, 수락) → `stage` → 차트 재생성 →
임시 브랜치 push → Argo가 그 Service를 지웠다(Synced/Succeeded). 임시 브랜치와 클러스터는 지웠다. 이 과정에서 찾은 결함 셋
(CRD와 Application 동시 설치, namespace 미생성, ApplicationSet 끄는 키)은 4bc41c87에서 고쳤다.

**권한은 1단계로 넉넉하게** 둔다(해피패스를 쉽게): AppProject는 이 저장소 하나, 이 클러스터, 모든 namespace·종류를 허용하고
Argo는 클러스터 범위 권한으로 돈다. 해피패스가 안정되면 좁힌다.
1. `destinations`를 원하는 상태에 나오는 namespace로, `clusterResourceWhitelist`를 `ProxyGroup`으로, `namespaceResourceWhitelist`를
   `Service`·`NetworkPolicy`(+ 모듈이 늘면 그 종류)로 줄인다.
2. Argo controller의 ClusterRole을 같은 종류로 줄인다(플랫폼 Argo처럼 `createClusterRoles: false` + 전용 Role).
3. `targetRevision`을 브랜치 대신 서명·검증된 커밋으로 고정하고, 승격은 PR로 한다.
4. `prune`은 모듈별로 켜고, 삭제 전 plan의 `delete` 변경을 사람이 승인한다.

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

- 비밀 값이 필요한 모듈의 GitOps 적용(ESO·OpenBao로 주입). 버킷 공개 상태는 지금 T9 `object_exposure.py plan`의 운영 순서로
  적용하고, GATE가 IAM-01.1을 다시 대조한다.
- 실제 고객 클러스터에 Argo 설치(목적지·출발지 담당과 시점 합의).
- 에이전트 도구 `infra.plan`(T11)과 플랫폼 API 연결(T10). 이 패키지는 둘 다 네트워크 없이 부를 수 있게 순수 함수로 둔다.
- 모듈 추가(목적지 앱 Deployment·NetworkPolicy·OpenBao 정책 등), 목적지 쪽 tailnet 노출(API proxy·Pooler·DB의 태그).
- tailnet 정책의 GitOps 적용(PR에서 Tailscale validate, 머지 때 apply)과 기존 경로 렌더러의 이 모듈로 이전.
