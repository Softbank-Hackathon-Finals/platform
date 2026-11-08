# [ADR-0035] tailnet 합류·정책·분리를 코드로 하고 Tailscale 인증은 워크로드 신원 연합(WIF)으로 한다

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T48, T44(ADR-0032), T45(ADR-0033), ADR-0034(#650, 환경 종류), T2 · T30, #627 · #643(operator-access · tailnet-api-proxy)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 2026-11-07 맥미니에서 정본 T2 생성기로 새 RKE2 시험 클러스터를 세우고, T44 고객 Argo CD와 T45 실행기로 DB · 객체 저장소 · ESO까지 코드로 올렸다. 그 뒤 tailnet 합류 단계에서 멈췄다.
* **문제:**
  * **tailnet 정책을 손으로 고친다.** 저장소에는 `bravo-iac tailnet-policy`가 만든 조각(`infra/tailnet/routes-policy*.json`)만 있다. 관리자가 콘솔에서 합친다. 실제 정책과 저장소가 이미 다르다(예: 실제 정책에만 있는 `svc:bravo-target-credentials` 자동 승인).
  * **operator 자격이 사람 손을 거친다.** 플랫폼 · 대상 operator의 `operator-oauth` Secret(OAuth client secret)을 사람이 만들었다. T45 스택도 `tailscale-oauth`를 "사람이 메모리에서 전달"하는 단계로 둔다. 키 관리 인프라(대상 OpenBao · ESO, 출발지 Secrets Manager · ESO)를 거치지 않고, 플랫폼 쪽에는 이 키를 둘 저장소가 없다(`infra/envs/platform-aws/main.tf`의 노드 역할은 Secrets Manager 읽기를 거부한다).
  * **합류 시점이 정해져 있지 않다.** VM 생성 때는 Tailscale을 설치만 하고 로그인은 사람이 한다(T30).
  * **분리 코드가 없다.** 작업이 끝나거나 고객이 요청해도 연결을 닫는 단계가 없다.
* **목표:** 새 클러스터가 세워질 때 tailnet에 합류하고, 플랫폼이 그 경로로 고객 클러스터를 제어(Argo CD 배치)한다. 고객과 운영자는 tailnet 신원으로 바로 붙는다. 끝나면 연결을 닫는다. 이 전 과정을 Git과 승인으로 관리하고, 사람이 비밀 값을 다루지 않는다.

## 2. 고려한 옵션들 (Considered Options)

### 1. OAuth client secret을 키 관리 인프라에 보관

플랫폼 키 저장소(OpenBao 또는 Secrets Manager)에 환경별 OAuth client secret을 두고, 실행기가 단계 시점에 읽어 `operator-oauth`로 넣는다.

**Pros**
* 지금 operator 설치 방식을 그대로 쓴다.

**Cons**
* 장기 secret이 남는다. 저장소 · 회전 · 회수 절차를 새로 만들어야 한다.
* 플랫폼에 키 저장소가 아직 없다.

### 2. 워크로드 신원 연합(WIF)으로 secret을 없앤다

Tailscale에 "이 OIDC 발급자의 이 주체(ServiceAccount 또는 CI 작업)"를 신뢰하는 연합 신원을 만든다. operator · CI는 자기 OIDC 토큰으로 인증한다. Tailscale operator(1.92.3 이상, 우리는 1.104.4), GitOps 정책 액션, Terraform provider가 모두 지원한다.

**Pros**
* 보관 · 회전할 secret이 없다. 연결 닫기는 연합 신원 삭제다.
* 연합 신원은 API와 Terraform provider(`tailscale_federated_identity`)로 만들고 지울 수 있다. 정책과 같은 코드로 관리된다.

**Cons**
* Tailscale이 클러스터의 OIDC discovery 문서와 공개키를 인터넷에서 읽을 수 있어야 한다. 온프렘 클러스터는 밖에서 닿지 않으므로 발급자를 공개 정적 주소로 따로 둔다(공개키만, 비밀 값 없음).
* 처음 Terraform을 실행할 신원 하나는 관리자가 만든다.

### 3. 정책 적용 방식: `tailscale/gitops-acl-action` 단독

정책 파일만 액션으로 적용한다.

**Pros**
* 공식 액션이고 단순하다.

**Cons**
* 연합 신원은 다루지 않아 합류 · 분리가 다른 경로로 갈린다.

## 3. 결정 사항 (Decision Outcome)

* **2. WIF를 선택한다.** Tailscale 인증에 장기 secret을 쓰지 않는다. WIF로 안 되는 키만 키 관리 인프라에 두고 실행기가 넣는다. 사람이 값을 붙여 넣지 않는다.
* **정책을 쓰는 곳은 `tailnet-policy` 워크플로 하나다(#667, 2026-11-07 20:5x 갱신).** 처음에는 Terraform `tailscale_acl`로 정책 전체를 쓰려 했으나 버린다. 정책 전체에는 그룹 구성원 이메일이 있어 공개 저장소에 둘 수 없고, 두 곳에서 쓰면 서로 덮어쓴다. 이 워크플로는 임시 경로가 아니며, 정책 쓰기를 Terraform으로 옮기지 않는다(#667 리뷰 1).
  * 원본은 `infra/iac/environments/*.json`에서 만든 조각이다. 조각을 실제 정책에 더한다(같은 허용이 이미 있으면 더하지 않음). 기준 커밋(마지막으로 성공한 develop run)의 조각에서 빠진 항목은 실제 정책 값이 정확히 같을 때 지운다(prune, #680). 정확히 같은 항목은 출처와 무관하게 저장소 소유로 본다. 사람이 바꾼 항목은 남기고 `kept`로 보고한다. 조각이 다루지 않는 그룹 · ssh · 그 밖의 grant는 그대로 둔다.
  * develop head와 워크플로가 읽는 경로가 다른 run은 쓰지 않는다(`IAC_STALE_RUN`). 옛 run을 Re-run해서 그 사이 되돌린 항목을 지우는 일을 막는다.
  * **정책 쓰기는 cluster-admin 경계다.** #627 · #643 구조에서 `tailscale.com/cap/kubernetes` grant 하나가 고객 클러스터의 cluster-admin을 준다. 그래서 적용 신원을 다음처럼 좁힌다. 둘 다 발급자는 `https://token.actions.githubusercontent.com`이고, subject는 이 저장소의 ID 고정 형식으로 정확히 고정한다(와일드카드 없음). 태그는 없다.
    * 계획: `repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:environment:tailnet-policy-plan`, 범위 `policy_file:read`. PR에서 읽기 · 합치기 · `tailnet-check` · Tailscale validate만 한다.
    * 적용: `…:environment:tailnet-policy`, 사용자 claim `ref=refs/heads/develop`, 범위 `policy_file`. 환경 `tailnet-policy`는 develop만 배포하고 필수 승인자를 둔다. develop은 승인 0이므로 병합만으로 적용되지 않는다(ADR-0032와 같은 기준).
  * 잠김 방지: 쓰기 전에 Tailscale validate PASS가 필요하고, Tailscale은 정책 `tests`가 깨지는 저장을 거부한다. 개발 기간 운영자 접근(`autogroup:admin` → API 프록시)은 관리자 역할 여부가 Git에 없어 사람별 시험을 두지 않는다. 대신 operator 태그 경로의 accept 시험을 조각에 둔다.
  * 긴급 수정(break-glass, 절차는 `infra/tailnet/README.md`): 적용은 환경 승인 거부나 `gh workflow disable tailnet-policy`로 멈춘다. 콘솔에서 지운 grant는 Git에 남아 있으면 다음 적용이 다시 더한다. 그래서 회수는 콘솔에서 지운 뒤 그 경로를 Git에서 빼는 순서로 한다. 적용 신원이 의심되면 연합 신원을 Revoke한다. 읽은 뒤 정책이 바뀌면 `IAC_POLICY_CHANGED`로 멈추고 덮어쓰지 않는다.
* **환경별 operator 연합 신원은 Terraform 스택 `infra/envs/tailnet`이 만든다(#662).** `tailscale_federated_identity`를 환경마다 하나 둔다. 발급자는 그 클러스터의 ServiceAccount 발급자, 주체는 `system:serviceaccount:tailscale-system:operator`, 태그는 `tag:bravo-<env>-operator`, 범위는 operator에 필요한 Devices Core · Auth Keys · Services 쓰기다. 이 스택은 정책을 쓰지 않는다. 태그 소유자는 신원보다 먼저 `tailnet-policy` 조각으로 넣는다.
* **온프렘 발급자를 공개 정적 주소로 둔다.**
  * T2 cloud-init이 RKE2 `service-account-issuer`와 `service-account-jwks-uri`를 그 주소로 렌더한다. 기존 클러스터 내부 발급자는 두 번째 발급자로 남겨 기존 토큰을 깨지 않는다.
  * 플랫폼이 discovery 문서와 공개키만 플랫폼 관리 정적 호스팅(S3, 객체 단위 공개 읽기)에 올린다.
  * EKS · GKE는 자체 공개 발급자를 쓴다(ADR-0034의 종류별 어댑터 칸).
* **연결 수명은 이 순서다.**
  1. **합류:** 환경 원하는 상태에 tailnet 항목을 넣는 PR → `tailnet-policy` 적용(승인) → 연합 신원 적용 → T45가 operator를 WIF 값(`oauth.clientId`, `oauth.audience`)으로 설치 → operator가 `tag:bravo-<env>-operator`로 합류.
  2. **제어:** 플랫폼과 운영자는 그 클러스터의 auth API 프록시로 들어온다. 개발 기간에는 운영자에게 `bravo:operators` → `cluster-admin`을 준다(11/7 사용자 결정, #627 · #643). 프록시는 operator가 선 뒤에 생긴다. 그래서 빈 클러스터의 T45 앞 단계(Tailscale 단계 전)는 VM 로컬 kubeconfig로 실행하고, Tailscale 단계 뒤부터 프록시로 바꾼다(#659 리뷰 (a)). 그 뒤로는 포트포워딩 · SSM · 개인 kubeconfig를 쓰지 않는다.
  3. **고객 접근:** IdP 그룹을 그 환경의 namespace 역할에 묶는다. 고객 그룹은 다른 환경에 grant가 없다.
  4. **분리:** 작업이 끝나거나 고객이 요청하면 그 환경의 tailnet 항목을 지우는 PR을 낸다. 승인 뒤 `tailnet-policy`가 그 정책 항목을 지우고(prune), 연합 신원 스택이 신원을 지운다. 이어서 장치를 지우고, operator 인증 실패와 플랫폼의 도달 실패를 확인해 분리 영수증을 남긴다.

## 4. 결과

* **바뀌는 것(담당 확인 필요):**
  * T48(류지환): `infra/envs/tailnet` 연합 신원 스택과 그 CI, `tailnet-policy`의 prune(#680), 분리 확인, 이 ADR.
  * T44(이상무): `tailnet-policy` 워크플로와 조각(#667)을 그대로 소유한다. 정책을 쓰는 곳은 이 하나다.
  * T45(전동훈): `tailscale-oauth` 단계를 연합 신원 확인 단계로 바꾸고 operator 값을 WIF로 렌더한다. 2026-11-07 시험에서 찾은 고정값 두 개(`object-secrets`의 원하는 상태 파일이 `target-dev.json` 고정, Tailscale 단계의 `--side target` 고정)를 환경 기준으로 바꾼다.
  * T2 · T30(김태현): cloud-init의 ServiceAccount 발급자 렌더.
* **기존 환경:** 운영 중인 플랫폼 · 대상 operator는 별도 실행 창에서 WIF로 옮긴다. 이 ADR만으로 기존 Secret을 지우지 않는다.
* **실측으로 확인됨(2026-11-08 KST):**
  * **합류 순서와 토큰 교환:** bravo-t49(온프렘 RKE2)에서 다음 순서로 확인했다. 각 단계가 끝난 뒤에 다음을 진행했다.
    1. 공개 발급자 게시(#723)
    2. API 서버 발급자 drop-in(#753, 공개 발급자는 첫 번째로 서명하고 내부 발급자는 두 번째로 수용)
    3. 태그 소유자·deny 시험(#735, #671 적용)
    4. 연합 신원(콘솔, #662 형식)
    5. T45 `tailnet=wif`(#717)
    결과: 장치 `bravo-t49-operator`가 `tag:bravo-t49-operator`로 Connected였고, 그 장치는 t49 연합 신원이 만든 것이었다(콘솔 `oauthclient:` 필터). 같은 방식으로 GKE(`target-gke`, Google 공개 발급자)도 합류했다.
  * **audience:** Tailscale이 생성한 `api.tailscale.com/<client id>`를 쓴다. chart 1.104.4는 `oauth.audience`로 projected token을 만들고 `oauth.clientId`로 교환한다. oauth Secret은 없다(#717).
  * **JWKS를 쓰는 주체:** 공개키는 Git(`infra/envs/oidc-issuer/issuers/<env>/jwks.json`)에서만 오고, 쓰는 주체는 그 스택의 적용자뿐이다. 비밀 멤버가 있으면 적용이 막힌다(#723).
  * **공개 접근 차단:** 계정 수준 설정이 없고, 버킷 정책이 환경마다 문서 2개만 연다. 나머지·목록·평문 HTTP는 403이다.
  * **operator 태그의 도달:** 새 operator 태그(t49·target-gke·target-gce)를 출발지로 하는 grant·acl·ssh는 0개다. deny 시험 7개(수집기, 출발지·목적지 DB와 풀러, 다른 operator)는 정책을 저장할 때마다 Tailscale이 검증한다.
  * 발급자를 바꾸는 노드 적용은 rke2-server 재시작 중 포트 경합을 충돌로 오판해 한 번 되돌렸다. 이 오판은 #774로 고쳤다.
* **남은 확인:**
  * ADR-0033 표 9행과 `operator_action`의 "Tailscale OAuth 입력" 예시를 #717(wif 단계, `tailscale-joined` 관찰)에 맞춘다. `object-secrets`의 원하는 상태가 `target-dev.json`에 고정된 것을 환경 기준으로 바꾼다(T45). `tailscale-joined`가 operator 기동 직후에 읽는 경합을 대기·재시도로 고친다(#717 후속).
  * 원본 일원화: `identities.json`과 환경 원하는 상태가 따로 있다. 신원만 지우면 grant · 태그 소유자가 남으므로, 신원 목록을 환경 원하는 상태에서 만들거나 두 파일의 일치를 시험으로 고정한다.
  * 연합 신원 스택의 CI와 첫 실행용 신원(`federated_keys`)의 관리자 절차. 그 전까지 t49·target-gke 신원은 콘솔에만 있다(`docs/legacy.md` 레거시 후보).
  * 분리: 장치를 지울 신원(`devices:core`), 이미 발급된 토큰(최대 1시간)과 키 만료를 끈 장치 처리, 고객 쪽 operator · CRD · ProxyGroup 제거를 신원 삭제보다 먼저 하는 순서. 실제 분리 시험은 아직 하지 않았다.
  * 연합 신원을 만드는 신원이 자기보다 넓은 범위 · 태그의 신원을 만들 수 있는지(Tailscale이 부분집합만 허용하는지).
* 이 결정은 해피패스 이후 고도화다. 설계 문서 반영은 버전을 올려 따로 한다.
