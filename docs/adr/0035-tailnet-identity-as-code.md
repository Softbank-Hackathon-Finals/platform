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
* **tailnet 설정은 Terraform 스택 `infra/envs/tailnet` 하나로 관리한다.** 옵션 3 대신 이 스택이 정책과 연합 신원을 함께 다룬다.
  * `tailscale_acl`: 전체 정책. 원본은 Git의 기본 정책과 `infra/iac/environments/*.json`에서 만든 조각이다. 사람 출처는 `autogroup:admin` · `autogroup:owner`와 IdP 그룹 이름만 쓴다. 이메일은 Git에 두지 않는다.
  * `tailscale_federated_identity`: 환경마다 하나. 발급자는 그 클러스터의 ServiceAccount 발급자, 주체는 `system:serviceaccount:tailscale-system:operator`, 태그는 `tag:bravo-<env>-operator`, 범위는 operator에 필요한 Devices Core · Auth Keys · Services 쓰기다.
  * PR에서는 읽기 · 검증 신원(`policy_file:read`)으로 계획과 정책 시험만 돈다. 적용은 보호된 GitHub Environment의 사람 승인 뒤 적용 신원으로 한다. develop은 승인 0이므로 병합만으로 적용되지 않는다(ADR-0032와 같은 기준).
  * 적용 결과와 다른 콘솔 수정은 다음 적용 때 덮어쓴다. 처음 적용 전에 실제 정책을 이 원본으로 옮긴다(이메일 제거 포함).
* **온프렘 발급자를 공개 정적 주소로 둔다.**
  * T2 cloud-init이 RKE2 `service-account-issuer`와 `service-account-jwks-uri`를 그 주소로 렌더한다. 기존 클러스터 내부 발급자는 두 번째 발급자로 남겨 기존 토큰을 깨지 않는다.
  * 플랫폼이 discovery 문서와 공개키만 플랫폼 관리 정적 호스팅(S3, 객체 단위 공개 읽기)에 올린다.
  * EKS · GKE는 자체 공개 발급자를 쓴다(ADR-0034의 종류별 어댑터 칸).
* **연결 수명은 이 순서다.**
  1. **합류:** 환경 원하는 상태에 tailnet 항목을 넣는 PR → 정책 · 연합 신원 적용(승인) → T45가 operator를 WIF 값(`oauth.clientId`, `oauth.audience`)으로 설치 → operator가 `tag:bravo-<env>-operator`로 합류.
  2. **제어:** 플랫폼과 운영자는 그 클러스터의 auth API 프록시로 들어온다. 개발 기간에는 운영자에게 `bravo:operators` → `cluster-admin`을 준다(11/7 사용자 결정, #627 · #643). 펼치는 작업도 Tailscale 관리자 신원으로 이 경로를 쓴다. 포트포워딩 · SSM · 개인 kubeconfig를 쓰지 않는다.
  3. **고객 접근:** IdP 그룹을 그 환경의 namespace 역할에 묶는다. 고객 그룹은 다른 환경에 grant가 없다.
  4. **분리:** 작업이 끝나거나 고객이 요청하면 그 환경의 tailnet 항목을 지우는 PR → 적용(삭제가 있으므로 사람 승인)으로 연합 신원과 정책 항목이 사라진다. 이어서 장치를 지우고, operator 인증 실패와 플랫폼의 도달 실패를 확인해 분리 영수증을 남긴다.

## 4. 결과

* **바뀌는 것(담당 확인 필요):**
  * T48(류지환): `infra/envs/tailnet` 스택, 정책 합성, CI 계획 · 적용 작업, 분리 확인, 이 ADR.
  * T44(이상무): 정책 조각을 전체 정책 합성의 입력으로 쓰고, T44 할 일 "tailnet 정책 GitOps"를 T48로 옮긴다.
  * T45(전동훈): `tailscale-oauth` 단계를 연합 신원 확인 단계로 바꾸고 operator 값을 WIF로 렌더한다. 2026-11-07 시험에서 찾은 고정값 두 개(`object-secrets`의 원하는 상태 파일이 `target-dev.json` 고정, Tailscale 단계의 `--side target` 고정)를 환경 기준으로 바꾼다.
  * T2 · T30(김태현): cloud-init의 ServiceAccount 발급자 렌더.
* **기존 환경:** 운영 중인 플랫폼 · 대상 operator는 별도 실행 창에서 WIF로 옮긴다. 이 ADR만으로 기존 Secret을 지우지 않는다.
* **남은 확인:**
  * 공개 발급자에 대한 실제 Tailscale 토큰 교환.
  * 연합 신원의 `audience` 처리(Tailscale 생성 권장, operator Helm 값과의 대응).
  * 처음 Terraform 실행용 신원을 만드는 관리자 절차.
  * 정적 호스팅 계정의 공개 접근 차단 설정과의 충돌 여부.
* 이 결정은 해피패스 이후 고도화다. 설계 문서 반영은 버전을 올려 따로 한다.
