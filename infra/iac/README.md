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

| `tailnet-egress` v1 | 클러스터의 Tailscale egress 경로: 그룹마다 egress ProxyGroup(전용 태그), tailnet 호스트로 가는 ExternalName Service, 그 그룹 proxy Pod에 묶은 NetworkPolicy | `owner_tag`, `groups[]`: `name`, `tag`, `tailnet`?, `proxy_class`?, `client_policy`?(`false`만), `clients[]`(namespace·labels), `routes[]`(service·namespace·destination·destination_tag·port·port_name) |
| `tailnet-ingress` v1 | 클러스터 안 워크로드를 tailnet 장치로 노출: 장치마다 operator expose Service(호스트 이름·태그·selector·포트). 누가 닿는지는 소비자 쪽 `tailnet-egress` grant가 정한다 | `owner_tag`, `devices[]`: `service`, `namespace`, `hostname`, `tag`, `tailnet`?, `selector`, `ports[]`(name·port·target_port) |
| `operator-access` v1 | 사람이 클러스터 API에 들어오는 길: 바인딩마다 쿠버네티스 그룹 하나의 ClusterRoleBinding, 그리고 tailnet 그룹이 그 클러스터의 Tailscale API 프록시(443)에 닿아 그 쿠버네티스 그룹으로 impersonate되게 하는 grant 하나 | `proxy_hostname`, `proxy_tag`, `bindings[]`: `name`, `group`(`접두사:이름`, `system:` 금지), `cluster_role`, `tailnet_groups`(`group:…`, `autogroup:admin`, `autogroup:owner`); 선택 `tailnet` |
| `api-transport-access` v1 | 기존 API 프록시의 HTTPS 도달 권한만 선언. Kubernetes 객체·RBAC·impersonation은 만들지 않음 | `proxy_hostname`, `proxy_tag`, `tailnet_groups`(`group:…`, `autogroup:admin`, `autogroup:owner`); 선택 `tailnet` |
| `cluster-audit` v1 | API 서버 감사 정책을 **노드 파일**로: `/etc/rancher/rke2/audit-policy.yaml`(0600). 내용은 T30·T34가 검토한 정책(`services/control-runner/templates/rke2-audit-policy.yaml`)과 바이트가 같다(시험 고정). 바뀌면 API 서버 재시작 | `runtime`: `rke2`(EKS 등 관리형 제어면은 `IAC_RUNTIME_UNSUPPORTED`), `policy`: `metadata-v1` |

`target-object-exposure`는 T9 렌더러(`infra/envs/target-workloads/object_exposure.py`)와 같은 계정·권한을 낸다.
같은지는 시험이 고정한다. 계정 키는 Git에도 plan에도 없다. 적용하는 쪽이 적용 시점에 넣는다(`secret_inputs`).

`environments/target-dev.json`은 지금 **`open-demo`**다. 데모 1번(IAM-01.1: 인증 없이 첨부를 읽을 수 있는가)의
일부러 심은 결함이며 `demo_run_id`로 그 실행을 밝힌다. 운영 기본값은 `closed`다.

## 네트워크 경로를 미리 깐다 (`tailnet-egress`, `tailnet-ingress`)

**네트워크 API는 이것 하나다.** 플랫폼·출발지·목적지의 tailnet 경로는 모두 `environments/*.json`에 선언하고,
`GET /v1/iac/network`(CLI `bravo-iac network infra/iac/environments/*.json`)가 한 번에 돌려준다.

| 환경 | egress (클러스터 안 Service → tailnet 장치) | ingress (tailnet 장치 → 클러스터 안) |
|---|---|---|
| platform | 반영·리허설·Collector → 목적지 API(워크로드 tailnet), 컷오버 → 양쪽 DB·제어 Pooler·API | 아직 없음: 지표 저장소 `bravo-metrics` 노출은 저장소에 렌더러가 없어 다음 단계에서 선언 |
| source-dev | Collector → 지표 저장소, 복제(역방향 구독) → `bravo-target-db` | `bravo-source-db`, `bravo-source-control-pooler` |
| target-dev | Collector → 지표 저장소, 복제(정방향 구독)·Pooler → `bravo-source-db` | `bravo-target-db`, `bravo-target-control-pooler` |

응답에는 환경마다 경로(호출할 클러스터 안 DNS 이름·포트 → 장치·태그·tailnet, 호출자)와 노출 장치, tailnet마다
정책 조각이 들어간다. 커밋된 파일만 읽으므로 proxy Pod·포트·주소를 찾을 필요가 없다. 옛 렌더러에서 같은 객체를
만들던 명령(`target-workloads/network.py ingress`·`egress`, `executor-network/control_network.py pooler-ingress`,
#608의 명령들)은 종료 코드 2로 거부한다. operator 설치 값(`values`)만 렌더러에 남는다.

- 복제·Pooler 그룹은 `client_policy: false`다. CNPG 인스턴스와 PgBouncer는 자기 DB·백업·API 서버로도 나가야 하므로
  클라이언트 쪽 egress를 격리하지 않고, proxy 쪽만 그 클라이언트로 제한한다. 이 값을 끄는 변경은 에이전트에게 확대다.

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
- **tailnet이 둘이다.** 플랫폼의 반영·리허설·Collector egress는 워크로드 tailnet의 `bravo-target-api`, 컷오버 egress는 사람·운영자 tailnet의 장치를 부른다(2026-11-07 13:00 KST 플랫폼 readback). 다른 tailnet에 붙는 그룹은 `tailnet` 키(예: `workload`)를 갖고, Service에는 `<호스트>.{tailnet:workload}`, 차트 값은 `tailnets.workload`, 정책 조각은 `infra/tailnet/routes-policy.workload.json`(`--tailnet workload`)로 따로 생성한다. 키가 없는 그룹은 지금처럼 `tailnet` 값과 `routes-policy.json`을 쓴다.
- 에이전트는 경로를 더할 수 없고(도달 범위가 늘면 `IAC_WIDENING_REFUSED`) 뺄 수만 있다. 도달 대상에는 tailnet이 들어간다. 같은 호스트 이름이라도 다른 tailnet이면 다른 장치이므로, 그룹의 `tailnet`을 바꾸는 것도 확대다. 그룹 `tag`를 바꾸는 것처럼 새 tailnet grant가 생기는 변경도 확대다.

**실검증은 Tailscale로**: `bravo-iac tailnet-verify infra/iac/environments/*.json`이 이 기기의 `tailscale status --json`으로
각 목적지·proxy 그룹을 판정한다(OK / OFFLINE / TAG_MISMATCH / NOT_VISIBLE). NOT_VISIBLE은 이 기기의 grant로 보이지
않는다는 뜻이며 경로 실패가 아니다. 전체 확인은 tailnet 장치 목록이 보이는 운영 기기에서 한다.
`bravo-iac tailnet-check --policy <현재 tailnet 정책 JSON>`은 현재 정책에 빠진 태그 소유·grant·tests를 코드로 낸다.
**정책 적용도 Git에서**: 관리 콘솔에 조각을 손으로 붙이지 않는다. `tailnet-policy` 워크플로가 PR에서 조각을 현재 정책에
합친 결과를 검사하고, develop 병합 뒤 그 결과를 적용한다(`bravo-iac tailnet-sync`). 더하기만 하고 지우지 않는다.
순서·설정은 [infra/tailnet/README.md](../tailnet/README.md#적용-tailscale-gitops).

2026-11-07 이 맥에서 처음 실행했을 때 목적지 API 장치 `bravo-target-operator`의 실제 태그(`tag:bravo-target-operator`)가
당시 선언(`tag:bravo-target-api`, 저장소 조각과 같음)과 **TAG_MISMATCH**였다. 선언을 실제 태그로 맞추고 정책 조각을 다시 생성해
지금은 OK다. API proxy에 전용 태그를 줄지는 목적지 담당이 정하고, 그때 같은 선언에서 바꾼다(`bravo-team-api-0`도 같은 태그).
나머지 11개는 이 기기 grant로 NOT_VISIBLE.

## 사람의 클러스터 접근 (`operator-access`)

사람은 SSM 포트포워딩이 아니라 **그 클러스터의 Tailscale API 프록시**로 API에 들어온다. 프록시는 auth 모드다
(operator 값 `apiServerProxyConfig.mode: "true"`, `operator-access` 프록시에 한해 `noauth` 금지. 기존 인증을 넘기는 전송 프록시는 아래 `api-transport-access`). 요청자의 tailnet 신원을 받아, grant가 이름 붙인
쿠버네티스 그룹으로 impersonate한다. 권한은 그 그룹의 ClusterRoleBinding이 정한다.

- 플랫폼: `autogroup:admin`·`autogroup:owner`와 등록된 `group:bravo-operators` → `bravo-platform-operator`
  (`tag:bravo-platform-operator`):443 → 쿠버네티스 그룹 `bravo:operators` → 현재 개발 환경의 `cluster-admin`.
  사람별 멤버 목록은 비공개 tailnet 정책에서 관리한다. 초대나 장치 표시만으로 운영자 자격을 인정하지 않는다.
  기존 개인 클러스터 권한을 확인한 사람을 운영자 그룹에 연결할 수 있으며, 접속을 위해 tailnet 관리자 역할로 승격할 필요는 없다.
  현재 개발 관리자 권한은 최소 권한 운영 인수의 완료를 뜻하지 않는다.
- 출발지는 정책의 `group:…` 또는 역할 autogroup(`admin`, `owner`)만 받는다. `autogroup:member`는 받지 않는다(초대만 받은
  사람까지 운영자가 된다). 누가 그 역할인지는 Git에 없으므로 이 grant의 정책 tests는 만들지 않고, `tailnet-check`가 grant
  (포트와 impersonate 그룹까지)를 대조한다.
- **왜 쿠버네티스 그룹을 거치나.** 프록시는 tailnet 신원을 쿠버네티스에 "사용자 이름(로그인)"과 "grant가 정한 그룹"으로만
  넘긴다. 권한을 주는 방법은 셋이다. 로그인마다 바인딩(공개 저장소에 이메일), `system:masters`로 impersonate(RBAC를 건너뛰어
  바인딩으로 좁히거나 회수할 수 없다), 이름 있는 그룹 하나에 바인딩. 마지막이 이름 없이 한 곳(ClusterRoleBinding)에서 권한을
  정하고 바꾼다.
- `system:` 그룹(특히 `system:masters`)은 impersonate하지 않는다. RBAC를 거치지 않고 권한을 주기 때문이다.
- 바인딩 추가, 역할·출발지·tailnet 변경은 에이전트에게 확대(`IAC_WIDENING_REFUSED`)다. 바인딩 삭제는 허용된다.
- **바인딩은 차트 밖에서 적용한다.** ClusterRoleBinding을 만드는 주체는 그 역할을 가져야 한다(RBAC 확대 방지). 그래서 생성 차트와
  그 Argo Application은 이 바인딩을 갖지 않고(`bravo.dev/unmanaged`), 클러스터 관리자가 `chart.held_objects()`의 렌더 결과를 적용한다.
  적용·변경·정리는 사람 몫이다. `bravo-iac held infra/iac/environments/platform.json > held.json` 뒤
  `kubectl diff --server-side --field-manager bravo-iac -f held.json`으로 보고 `kubectl apply --server-side --field-manager bravo-iac -f held.json`으로 적용한다.
- **역할을 바꾸면 새 바인딩이다.** `roleRef`는 바꿀 수 없으므로 이름은 `<그룹>-<역할>`(`bravo:operators` + `cluster-admin` →
  `bravo-operators-cluster-admin`)로 정해진다. 새 바인딩을 적용한 뒤 옛 바인딩은 사람이 지운다(prune 없음).
- `tailnet-check`는 선언된 API 프록시로 가는 **선언되지 않은** 접근도 보고한다. 정책 편집만으로 클러스터 권한이 생기지 않게
  하기 위해서다(프록시는 어떤 그룹이든 impersonate할 수 있다).
  - `UNDECLARED_GRANT`: 선언 밖 capability grant. 옛 키(`https://tailscale.com/cap/kubernetes`)도 capability로 본다.
  - `UNDECLARED_REACH`: 선언 밖 출발지가 프록시 443에 닿음(전송 프록시는 호출자의 자격을 그대로 넘긴다).
  - `SYSTEM_IMPERSONATION`: `system:` 그룹 impersonate. 대상과 상관없이 실패다.
  - dst의 `*`, tailnet 주소(`100.64.0.0/10`, `fd7a:115c:a1e0::/48`)와 그 주소의 host 별칭은 모든 선언 프록시로 본다. 태그·그룹·사용자·
    subnet route는 프록시가 아니다. 선언된 grant를 더 넓은 포트(`*`)로 적은 것은 같은 grant로 본다.
- `bravo-iac tailnet-verify`가 프록시 장치의 존재·태그·온라인을 `api-proxy` 행으로 판정한다. `GET /v1/iac/network`의
  `access`가 환경별 접근 경로(프록시, 출발지, 쿠버네티스 그룹, 역할)를 돌려준다.

사람의 순서(처음 한 번):

```sh
tailscale status | grep bravo-platform-operator          # 운영자 tailnet에서 프록시가 보이는지
tailscale configure kubeconfig bravo-platform-operator   # kubeconfig에 프록시 컨텍스트 추가(인증서·SSM 불필요)
kubectl auth whoami                                      # Groups에 bravo:operators가 있어야 한다
```

### 기존 Kubernetes 인증을 유지하는 API 접속 (`api-transport-access`)

`source-dev`는 `group:bravo-operators`에서 `tag:bravo-source-operator`의 TCP 443에 닿는 경로만 선언한다.
이 모듈은 새 RBAC, 인증서, 토큰 또는 impersonation capability를 만들지 않는다. 기존 Kubernetes/IAM 인증을
전달하는 프록시에 사용하며, auth 모드 프록시의 Kubernetes 권한을 부여하는 설정과 혼동하지 않는다.
실제 프록시 모드와 기존 인증으로의 조회가 확인되기 전에는 출발지 접속 성공으로 기록하지 않는다.
`GET /v1/iac/network`의 해당 `access` 행은 `authentication: existing-kubernetes-auth`로 구분한다.
같은 프록시 태그를 `operator-access`(auth)와 이 모듈이 함께 선언하면 의미가 반대라 `IAC_PARAMS_INVALID`로 거부한다.

이 경로의 인수 항목(장치 표시와 따로 본다):
1. 인증 없는 요청은 401이다(프록시가 인증을 대신하지 않는다).
2. 본인 IAM 토큰으로 `kubectl auth whoami`를 하면 IAM에 매핑된 본인 신원이 나온다(프록시 신원이 아니다).
3. TLS 검증을 켠 채로 접속한다(`insecure-skip-tls-verify` 없이 등록 CA와 서버 이름).

이 모듈과 `operator-access`의 사람 그룹은 멤버 목록을 Git에서 알 수 없으므로 태그 장치용 정책 tests를 만들지 않는다.
특히 플랫폼 `operator-access`의 `group:bravo-operators`는 멤버 전원에게 플랫폼 `cluster-admin` 경로를 준다. 멤버 추가는
PR 없이 정책 편집만으로 일어나고 `tailnet-check`는 멤버를 볼 수 없으므로 다음을 지킨다.
1. 기존 멤버가 모두 운영자인지 비공개 원장에서 확인한다.
2. 멤버를 바꿀 때는 `operator-access` 변경과 같은 수준의 승인을 받고 감사 기록을 남긴다.
3. 정책을 바꾸거나 이 저장소의 변경을 병합한 직후 실제 정책에 `tailnet-check`를 돌린다.
생성 조각은 전체 정책을 대체하는 파일이 아니다. 현재 정책을 비공개로 보존하고 필요한 grant만 중복 없이 합친 뒤
Tailscale의 정책 검증과 실제 조회를 수행한다. 다른 사람이 정책을 변경했다면 이전 복사본으로 덮어쓰지 않는다.

접속 인수는 장치 표시·Ready 여부와 분리한다. TLS 검증, 등록 클러스터 UID, 개인 `whoami`·그룹·권한을 확인한 뒤
대체된 세션만 종료한다. 온프렘용 개인 접속 도구와 플랫폼 kubeconfig를 혼용하지 않는다. 기존 SSM·포트포워딩은
직접 접속 인수 전까지 보존하고, 비상 복구 경로와 공개 서비스 HTTPS는 별도로 관리한다.

## 목적지 사람 접근 표준 (operator API 프록시 + `operator-access`)

사람은 목적지마다 **그 클러스터 Tailscale operator의 내장 API 프록시(auth 모드)** 하나로 들어온다. 플랫폼(`bravo-platform-operator`)과 같은 구조다. 새 프록시를 따로 두지 않는다. 포트포워딩·개인 토큰 없이 tailnet 신원으로 인증하고, 권한은 쿠버네티스 그룹 하나의 ClusterRoleBinding이 정한다. 워크로드용 noauth 프록시와 발급 프록시는 섞지 않는다.

1. operator 설치 값: API 프록시를 auth 모드로 켠다(`apiServerProxyConfig.mode: "true"`, 배포 env `APISERVER_PROXY=true`)와 impersonation RBAC(`tailscale-auth-proxy`).
   고정 차트 1.104.4 렌더 확인: `mode: "true"`는 내장 프록시와 RBAC(`kube-apiserver-auth-proxy` SA, `tailscale-auth-proxy`)를 함께 켠다.
   `allowImpersonation: "true"`만 켜면 RBAC만 생기고 내장 프록시는 꺼진다(별도 kube-apiserver ProxyGroup용). 이 표준은 내장 프록시를 쓰므로 `mode: "true"`다.
2. 원하는 상태: `operator-access`에 그 operator 장치(`proxy_hostname`·`proxy_tag`)와 바인딩을 둔다(`target-dev.json`: `bravo-target-operator` → `bravo:operators` → `cluster-admin`).
3. tailnet 정책: `bravo-iac tailnet-policy` 조각의 app capability grant를 현재 정책에 병합하고 `bravo-iac tailnet-check`로 대조한다.
4. 클러스터: 클러스터 관리자가 `chart.held_objects()`의 ClusterRoleBinding을 적용한다(차트·Argo 밖, 위 절).
5. 확인: `tailscale configure kubeconfig <operator 호스트>` 뒤 `kubectl auth whoami`에 `bravo:operators`가 보이고, 그룹이 없으면 거부되는지 본다.

**런타임 어댑터.** `operator-access`는 쿠버네티스 런타임마다 같다. 런타임이 다른 곳은 1번(operator 설치)과 그 실측이다.

| 런타임 | 상태 | 추가 전에 확인할 것 |
|---|---|---|
| `rke2` (온프렘) | 실측됨(2026-11-07: `APISERVER_PROXY=true`, tailnet 신원 → `bravo:operators`, 그룹만으로 허용·그룹 없음 거부) | — |
| `rke2` (T45로 새로 세운 목적지) | 아직 | 1번이 채워지지 않는다. T45 tailscale 단계가 쓰는 operator 값(`target-workloads/network.py`)이 `apiServerProxyConfig.mode: "false"`라 auth 프록시와 impersonation RBAC가 렌더되지 않는다. 설치 주체(T45 tailscale 단계 / T30 `network.py`)를 정하면 1번이 그 단계를 가리킨다. `network.py`는 T30 파일이라 담당 합의로 바꾼다 |
| `eks` (AWS) | 아직 | operator 설치 값과 impersonation RBAC, 노드에서 Tailscale 제어·DERP로 나가는 길, 위 5번 |
| `gke` (GCP) | 아직 | 같은 항목, Autopilot이면 ProxyClass 제약. 확인된 것(2026-11-07, A10 GKE Standard): 프로젝트 소유자(IAM)가 이 바인딩을 만들 수 있다(`kubectl apply --server-side --dry-run=server`, 저장하지 않음). 남은 것: A10 스택도 같은 `network.py` 값을 써서 프록시가 꺼진다. `--side target`이면 장치 이름·태그가 온프렘 목적지와 같은 `bravo-target-operator`가 된다 |

사람별 이메일 ClusterRoleBinding(2026-11-06 수동)은 쓰는 사람이 그룹 방식으로 확인한 뒤 레거시로 옮긴다.
## 노드 파일 (`cluster-audit`)

API 서버가 시작할 때 노드에서만 읽는 설정은 클러스터 안 Argo가 가질 수 없다. 그래서 모듈이 파일의 정확한 바이트를
내고, 노드의 적용기가 설치한다. 차트에는 들어가지 않고 `Chart.yaml`의 `bravo.dev/unmanaged`에 적힌다.

| 경로 | 뜻 |
|---|---|
| `infra/iac/nodes/<환경>/manifest.json` | 그 환경의 노드 파일 목록: 경로·모드·sha256·재시작 대상·모듈. `bravo-iac node`로 생성(손으로 고치지 않음, 시험이 일치를 고정) |
| `infra/iac/nodes/<환경>/<노드 경로>` | 파일 내용. 노드의 실제 경로 그대로(`etc/rancher/rke2/audit-policy.yaml`) |
| `infra/iac/nodes/apply.py` | 실행 중인 RKE2 서버 노드의 적용기(표준 라이브러리만) |

```sh
cd services/iac && uv run bravo-iac node ../../infra/iac/environments/platform.json --out ../../infra/iac/nodes/platform
# 노드에서(root): 묶음과 클러스터 UID를 대조하고, 바뀔 것만 보고한다
python3 apply.py <묶음 폴더> --cluster-uid <kube-system UID>
# 적용: 기존 파일 백업 → 원자적 설치 → API 서버만 재시작 → /readyz와 감사 로그 증가 확인
python3 apply.py <묶음 폴더> --cluster-uid <kube-system UID> \
  --sha256 /etc/rancher/rke2/audit-policy.yaml=<검토한 sha256> --apply   # GET .../node-files의 files[].sha256
```

- **묶음을 믿지 않는다.** 쓸 수 있는 경로·모드·재시작 대상은 적용기 코드의 허용 목록(`ALLOWED`)뿐이다. manifest가 다른
  경로를 적으면 쓰기 전에 거부한다. 바이트는 `--sha256`(검토한 `GET .../node-files`의 파일별 `sha256`)으로 묶고, `--apply`에는
  필수다. manifest의 sha와 `state_digest`는 묶음 안에서 베낄 수 있어 기준이 되지 못한다(`level: None`으로 바꾼 묶음도 거부).
  온프렘 user-data 조립기도 같은 허용 목록을 쓴다. 허용 목록이 노드 모듈 출력과 같은지 시험이 고정한다.
- **설치 뒤 무엇이 실패해도 되돌린다.** API 서버 준비 실패, 다른 예외, 감사 로그가 늘지 않는 경우가 모두 해당한다.
  이전 바이트와 모드를 되돌린 뒤 API 서버를 다시 기다리고, 그 결과를 `rollback_ready`로 보고한다. `false`면 API 서버가
  돌아오지 않은 것이니 운영자가 바로 본다. 되돌리기 쓰기 자체가 실패하면 `rolled_back: false`와 새 바이트가 남은 파일
  `rollback_failed`를 남긴다. 감사 로그가 그사이 회전하면(inode가 바뀜) 새 파일에 바이트가 있으면 증가로 본다.
- **ADR-0032의 "적용은 Argo"와 다른 길이다.** API 서버가 시작할 때 노드에서만 읽는 파일이라 클러스터 안 Argo가 가질 수
  없다. 그래서 노드 파일만 노드 적용기(첫 부팅은 user-data, 실행 중인 노드는 `apply.py`)가 적용한다.

- API(읽기 전용): `GET /v1/iac/environments/{id}/node-files`(목록과 각 파일의 `href`),
  `GET /v1/iac/environments/{id}/node-files/{노드 경로}`(내용과 sha256). 없는 경로는 404 `IAC_NODE_FILE_UNKNOWN`.
- 새 노드: 온프렘 user-data(`infra/envs/onprem/render-user-data.sh`)는 같은 원본을 첫 시작 전에 넣는다.
- 플랫폼 노드(`infra/envs/platform-aws`): `user_data_replace_on_change = true`와 `prevent_destroy`가 함께 있어,
  cloud-init을 바꾸면 다음 plan이 교체 거부로 멈춘다. 그래서 실행 중인 플랫폼 노드는 `apply.py`로 맞추고 cloud-init은
  바꾸지 않는다(새 플랫폼 노드를 만들 때 같은 묶음을 넣는다).
- 에이전트 계획은 이 모듈을 뺄 수 없다(`IAC_MODULE_SET_CHANGE_REFUSED`). 감사를 끄는 정책 프로필은 없다.

## 적용: 고객 클러스터마다 Argo CD (pull GitOps)

플랫폼 Argo CD가 여러 고객 클러스터를 밀어 넣지 않는다. **각 고객 클러스터에 Argo CD를 하나 두고**, 그 클러스터의
환경 한 곳(`infra/iac/charts/<환경>`)만 끌어와 적용한다. 고객 클러스터 수가 늘어도 플랫폼 쪽 부담·자격이 늘지 않고,
고객 클러스터의 API를 플랫폼에 열 필요가 없다.

| 경로 | 뜻 |
|---|---|
| `infra/iac/charts/<환경>` | `bravo-iac chart`로 생성한 Helm 차트(손으로 고치지 않음). 값은 `tailnet`(`<이름>.ts.net`), 다른 tailnet 그룹이 있으면 `tailnets.<키>`도 필수 |
| `infra/iac/argocd` | 고객 클러스터에 설치하는 Argo CD(argo-helm 10.14.0, 플랫폼과 같은 판, Core 모드: UI·SSO·ingress 없음) + AppProject `bravo-iac` + 그 환경의 Application |

```sh
# 차트 생성(원하는 상태가 바뀌면 같은 PR에서 다시 생성한다. 시험이 일치를 고정한다)
cd services/iac && uv run bravo-iac chart ../../infra/iac/environments/target-dev.json --out ../../infra/iac/charts/target-dev
# 고객 클러스터에 한 번 설치(운영자). tailnet 접미사는 이 명령의 값으로만 들어가고 Git에 남지 않는다
helm dependency build infra/iac/argocd
helm upgrade --install bravo-iac-gitops infra/iac/argocd -n bravo-iac-gitops --create-namespace \
  --kubeconfig <고객 전용 kubeconfig> \
  --set environment=target-dev --set tailnet=<이름>.ts.net --set targetRevision=<검토한 커밋의 전체 SHA> \
  --set application.enabled=false --set application.autoSync=false --set application.prune=false
```

**적용하는 커밋은 설치 값 `targetRevision`(전체 40자 SHA, 필수)이다.** 브랜치 기본값이 없어서 develop 병합만으로는 고객
클러스터에 닿지 않는다(develop은 승인 0이다). 승격은 검토한 커밋 SHA로 이 값 한 줄을 바꾸는 것이다.

**플랫폼 클러스터에는 이 차트를 설치하지 않는다.** 이미 T43 Argo가 있으므로 `charts/platform`은 그 Argo의 Application으로
붙인다(한 클러스터에 Argo 두 벌을 두지 않는다). `infra/iac/argocd`(CRD 설치 포함)는 고객 클러스터(`source-dev`, `target-dev`) 전용이다.
그 Application은 `infra/gitops/argocd`의 `iacNetwork`(기본 꺼짐)다. 켤 때 값은 검토한 전체 커밋 SHA(`targetRevision`)와
tailnet 접미사(`tailnet`, `tailnets.workload`)이고, AppProject `platform-iac-network`는 이 차트에 있는 namespace 5개와
종류(Namespace·ProxyGroup·Service·NetworkPolicy)만 허용한다(시험이 차트와 같은지 고정). 켜기 전에 컨트롤러 권한이 필요하다:
그 namespace의 Role·RoleBinding, Namespace·ProxyGroup 클러스터 접근, `clusterCredentials.platform`의 namespace·클러스터 자원,
`resource.inclusions`. 이 권한 작업은 따로 한다(T44 할 일 "권한 세분화").

**같은 자원의 주인은 하나다.** 이 모듈을 적용하는 클러스터에서는 같은 이름의 경로를 만드는 기존 렌더러를 쓰지 않는다:
`infra/envs/rehearsal-access/access.py`(리허설·반영·Collector → 목적지 API), `infra/envs/executor-network`(컷오버 경로),
`infra/envs/collector-network`(Collector → 지표 저장소). 두 쪽이 함께 적용하면 NetworkPolicy 규칙(관찰한 포트 대 10000–10999)이
서로 되돌린다. 렌더러의 경로 출력은 담당 합의로 이 모듈로 옮긴다(T44 할 일: T15·T12·T37).

이후 원하는 상태의 변경은 PR → 병합 → **검토한 커밋 SHA 승격** → 고객 Argo 동기화 순서다.
브랜치 병합만으로 적용되지 않는다. 자동 동기화는 명시적으로 켜고, 자동 삭제(prune)는 별도 결정한다.
조정 주기는 60초와 jitter 15초이며, Git 접근·렌더·적용 실패가 있으면 그 안에 반영된다고 보장하지 않는다.
비밀 값이 필요한 자원(지금은 버킷 계정 Secret)은 차트에 없고 `Chart.yaml`의 `bravo.dev/unmanaged`에 적힌다.
**차트는 Namespace를 선언하지 않는다.** namespace의 주인은 클러스터를 세우는 쪽이다. 같은 Namespace를 T44·T45 두 Application이 함께 관리하던 겹침(#620 kind 실측의 Argo 경고)을 없앤다. 차트가 자원을 두는 namespace와 그 주인은 다음과 같다.

| namespace | `target-dev` 차트(새 목적지) | `source-dev` 차트(출발지) |
|---|---|---|
| `target-dev` | T45 base 단계(PSA restricted) | — |
| `source-dev` | — | 출발지 렌더러(`infra/envs/source-workloads`) |
| `tailscale-system` | T45 tailscale-oauth 단계(PSA privileged) | 기존 Tailscale operator 설치 |
| `bravo-observability` | T45 base 단계(추가 요청 중, #632), T37 Collector와 같은 라벨(`component: platform`, PSA restricted) | T37 Collector 매니페스트(`services/observability/deploy/collector`) |

그래서 이 Application은 표의 namespace와 Tailscale CRD가 생긴 **뒤에** 켠다(새 목적지는 T45 base · tailscale 단계가 Synced · Healthy가 된 뒤, 아래 2단계). 그 뒤의 실패는 순서 문제가 아니므로 재시도는 유한하다(`retry.limit: 5`, 약 5분). 무제한 재시도는 실패한 operation을 Running으로 남겨, `targetRevision`을 고친 SHA로 올려도 새 동기화가 시작되지 않는다. 재시도가 끝나 Failed가 되면 원인을 고친 커밋을 승격하거나 수동으로 sync한다.
ProxyGroup은 그 클러스터에 Tailscale operator(CRD)가 있어야 생성된다(`SkipDryRunOnMissingResource`).

### 고객 설치와 업무 자원 적용의 분리 (차트 0.2.0)

실제 출발지/목적지 담당과 실행 시간을 맞추고 등록된 `kube-system` UID가 같은지 먼저 확인한다.
다른 Argo 설치·기존 렌더러·T45가 소유한 자원은 자동으로 인수하지 않는다. 플랫폼 클러스터에는 적용하지 않는다.

1. 위 명령은 `application.enabled=false`를 명시해 **Argo CD와 CRD만** 만든다. 기존 CRD가 있어도 AppProject·Application을 만들지 않는다.
2. 컨트롤러 준비와 CRD Established, 위 표의 namespace와 Tailscale CRD가 있는지 확인하고, 같은 설치 값에서
   `application.enabled=true`로 바꾼다.
   `application.autoSync=false`, `application.prune=false`는 유지한다. 고객 환경의 경로·diff·소유권을 확인할 수 있지만
   이 Application은 자동 적용하지 않는다. 다른 Application이나 수동 sync 권한을 제한하는 기능은 아니다.
3. 중복 소유권·tailnet 정책·설정 원문·검토 커밋과 적용 창을 확인한 뒤 `application.autoSync=true`로 승격한다.
   prune은 기본 false다. 제거할 자원의 목록과 영향이 승인된 경우에만 `application.prune=true`를 함께 준다.
4. 아래 읽기 전용 수집기로 실제 반영을 확인한다. DB·트래픽·운영 승인은 T10/T19/T12/T31의 기존 경로를 쓴다.

`helm upgrade`에는 매번 동일한 보호된 운영자 값 파일을 사용한다. 기존 0.1.0 설치는 자동 동기화·prune이 켜져 있었으므로
업그레이드 전에 세 모드 값을 명시적으로 정해야 한다. 기본값으로 덮어써서 기존 Application을 제거하지 않는다.
차트 0.2.2부터 `application.enabled`를 생략하면 렌더를 거부한다. bootstrap은 false, 기존 Application 유지·등록은 true를
명시한다. 스키마 검사를 건너뛰어도 같은 검사를 하며, 생략을 기본 false로 해석하지 않는다.
`autoSync=false`로 바꾸는 것만으로 이미 시작한 sync가 취소되지는 않는다. 실행 중이면 결과와 영향부터 확인한다.
초기 설치 권한은 아래의 넓은 1단계 권한이며, 설치·동기화 성공을 금융 운영 보안 자격으로 사용하지 않는다.

### 고객 GitOps 상태의 읽기 전용 인수

```sh
# 고객의 등록 원문에서 가져온 UID와 검토한 전체 SHA를 사용한다.
# 조회 결과에서 UID를 즉석 채택하면 대상 신원 검증이 되지 않는다.
python3 infra/iac/customer_status.py --kubeconfig <고객 전용 kubeconfig> \
  --expected-cluster-uid <등록한 kube-system UID> --environment target-dev \
  --revision <검토한 전체 커밋 SHA>
```

도구는 TLS 검증을 끄지 않고, `config view`와 GET만 실행한다. 환경·저장소·경로·프로젝트·로컬 목적지,
실제 sync revision과 설정, 실행 중 작업, 컨트롤러 준비, 최근 조정 시각, 조회 중 Application 변경을 확인한다.
고정 커밋이라도 Helm 값이 다른 오래된 성공은 수용하지 않는다. 명령 오류의 원문은 출력하지 않는다.

상태는 `NOT_INSTALLED`, `BOOTSTRAP_PENDING`, `BOOTSTRAP_ONLY`, `SYNC_PAUSED`, `SYNC_IN_PROGRESS`, `SYNC_RETRYING`,
`SYNC_FAILED`, `SYNC_PENDING`, `BLOCKED`, `SYNC_VERIFIED`로 구분한다. `SYNC_VERIFIED`만 종료 코드 0이고 나머지는 3이다.
실행 중(`Running`·`Terminating`) `retryCount > 0`이면 `SYNC_RETRYING`과 재시도 횟수를 표시한다. 새 operation이 아직 시작하지 않았으면
이전 작업의 횟수가 남아 있어도 `SYNC_IN_PROGRESS`다. 재시도가 끝나 이 커밋의 operation이 `Failed`·`Error`로 끝나면 `SYNC_FAILED`와 횟수를 표시한다. 재시도나 대기 상태를 인수 성공으로 처리하지 않는다.
로컬 시계보다 미래인 조정 시각은 기존처럼 거부한다. 시계 오차를 독립적으로 확인하지 않은 상태에서 유효 시간 검사를 완화하지 않는다.
승인된 prune을 별도로 인수할 때만 `--allow-prune`을 준다. 이 옵션은 삭제를 실행하거나 승인하지 않는다.
GitOps 수렴 확인만 기록하며 `authorizes_cutover`, `migration_verified`, `control_equivalence_verified`는 항상 false다.
T19의 통제 시험과 업무·데이터·무중단 검증은 별도로 필요하다.

차트 0.2.0의 격리된 arm64 설치·정지·고정 SHA 동기화와 실제 온프렘의 읽기 전용 인수 결과는
[고객 GitOps 검증 기록](../../docs/evidence/t44-customer-gitops-2026-11-07/README.md)에 있다.
현재 실제 고객 설치 완료를 뜻하지 않는다.

**아래는 차트 0.1.0 시절의 과거 실험 기록이다. 현재 고객 설치 절차로 사용하지 않는다.**
현재 절차에서는 namespace를 T45 등 클러스터 구축 담당이 먼저 만들며, `application.enabled`를 명시한다.

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
3. (적용됨) `targetRevision`은 검토한 커밋 SHA 필수. 다음 단계는 서명 검증(Argo GnuPG)과 승격 PR 자동화.
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
  `GET /v1/iac/environments/{id}/node-files[/{노드 경로}]`, `GET /v1/iac/network`, `POST /v1/iac/plan`. 커밋된 파일만 읽고 Git·클러스터에 쓰지 않는다. 호출자 인증은 앞단 호스트가 한다.

## 플랫폼 API: 계획 → T10 infra 승인 → 원하는 상태 PR

운영자 승인 호스트(`approval_server` + `operator_ports`)가 이전 계획(T10 plan)마다 제공한다(`iac_plans.py`).

| 순서 | 호출 | 하는 일 |
|---|---|---|
| 1 | `POST /v1/plans/{plan_id}/iac/plans` (`Idempotency-Key`, `{environment, module, params}`) | base 브랜치(develop)에 커밋된 원하는 상태로 결정적 계획. 응답의 `binding`이 승인 대상 |
| 2 | `POST /v1/plans/{plan_id}/approvals` (`approval_kind: infra`, 그 `binding`) | 다른 사람(`approve_infra`)이 그 `plan_digest`를 승인. 요청자는 승인할 수 없다 |
| 3 | `POST /v1/plans/{plan_id}/iac/plans/{iac_plan_id}/pull-request` (`{approval_id}`) | 지금의 base로 다시 계획해 digest가 같을 때만 승인을 한 번 소비(`apply_infra`)하고, 원하는 상태·생성 차트·tailnet 정책 조각을 담은 PR을 연다. 다시 부르면 같은 PR |
| 4 | PR 검토·병합 | 병합은 적용이 아니다. 검토한 커밋 SHA를 Argo `targetRevision`으로 줄 때 적용된다 |

base가 그 사이 바뀌어 digest가 달라지면 `iac_plan_stale`이고 승인은 소비되지 않는다(다시 계획·승인).
에이전트는 이 API를 부르지 않고 실행 안에서 `infra.plan`으로 계획한다(시도 기록 `infra_plans`).

운영 설정은 `/rehearsal/operator.json`의 선택 키 `iac`(`app_id`, `key_file`, `repository: Softbank-Hackathon-Finals/platform`)다.
**쓰기에는 GitHub App이 이 저장소에 contents·pull requests 쓰기로 설치되어 있어야 한다**(지금 App은 demo-chat 전용).
설치 여부와 권한은 운영 결정이라 이 저장소에서 바꾸지 않는다. 키가 없으면 경로가 생기지 않는다.

## 아직 아닌 것

- 비밀 값이 필요한 모듈의 GitOps 적용(ESO·OpenBao로 주입). 버킷 공개 상태는 지금 T9 `object_exposure.py plan`의 운영 순서로
  적용하고, GATE가 IAM-01.1을 다시 대조한다.
- 실제 고객 클러스터에 Argo 설치(목적지·출발지 담당과 시점 합의).
- 에이전트 도구 `infra.plan`(T11)과 플랫폼 API 연결(T10). 이 패키지는 둘 다 네트워크 없이 부를 수 있게 순수 함수로 둔다.
- 모듈 추가(목적지 앱 Deployment·NetworkPolicy·OpenBao 정책 등), 목적지 쪽 tailnet 노출(API proxy·Pooler·DB의 태그).
- tailnet 정책에서 조각에 없는 항목 지우기(prune). 경로를 Git에서 빼도 grant는 사람이 지운다. 기존 경로 렌더러의 이 모듈로 이전.
