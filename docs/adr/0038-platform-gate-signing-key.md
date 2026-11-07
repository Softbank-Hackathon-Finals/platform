# [ADR-0038] T19 게이트 서명 키를 플랫폼이 AWS KMS에 두고, workflow host가 워크로드 신원으로 서명한다

* **상태 (Status):** 채택됨(Accepted). 2026-11-07 22:3x KST 사용자 결정에 따른다.
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T19 #24, T14 #21, T49(사전 위임 #716), ADR-0031 workflow host, ADR-0035 공개 발급자, 운영자 발급자 결정(`operations/gate/run.py`, 11/7)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** T19 게이트는 context·T14 실행 기록·T13 준비 기록·게이트 결과마다 Ed25519 서명(envelope)을 요구한다. 11/7 결정은 "운영자 발급자 하나, 용도마다 키 하나"였다. 키는 운영자 Mac에 있고, 운영자가 `subjects`를 본 뒤 `sign --confirm`으로 서명한다.
* **문제:** 해피패스를 API 하나로 사람 개입 없이 돌리면, 서명이 남는 마지막 사람 단계가 된다. 사람의 결정은 이미 사전 위임(#716)으로 남는다. 서명은 "측정 결과가 플랫폼이 받은 그대로"라는 출처 증명이다.
* **사용자 결정 (원문):** "T19 서명키 위치는 플랫폼 서버나 파드에 두면 되지 않나? 이런 수평확장을 염두해둔 플랫폼 키는 어디두지? 현재 우리구조와 적합한 솔루션 찾아서 반영해." 41이 받은 선택지도 같다: "서명 키를 AWS 플랫폼(OpenBao/KMS)이 보관하고, T19가 측정 결과에 자동으로 서명합니다. 사람은 사전 위임(승인)만 합니다."
* **목표:**
  * 키는 AWS 플랫폼 쪽에만 둔다(21:5x 배치 기준).
  * replica가 몇 개든 같은 키로 서명한다.
  * 키 값이 파드·Secret·etcd·로그에 나타나지 않는다.
  * T19 검증기와 운영자 경로는 그대로 둔다.

## 2. 고려한 옵션들 (Considered Options)

### 1. Kubernetes Secret에 개인 키를 두고 workflow host에 마운트

**Pros**
* GitHub App 키와 같은 방식이라 바로 된다. replica마다 같은 Secret을 마운트한다.

**Cons**
* 키를 꺼낼 수 있다. `platform-state` Secret을 읽을 수 있는 모든 주체와 etcd 백업이 게이트 서명 권한이 된다.
* 회전·폐기·사용 기록이 없다. 누가 언제 서명했는지 남지 않는다.

### 2. 노드 IAM 역할(IMDS)로 KMS 호출

**Pros**
* 신원 연동을 새로 만들지 않는다.

**Cons**
* 플랫폼 노드는 IMDSv2 hop limit 1이라 일반 파드가 닿지 않는다(의도된 격리).
* hostNetwork 파드는 닿는데, 초기 제안 Job이 hostNetwork다. 노드 역할에 서명 권한을 주면 그 Job도 서명할 수 있다.

### 3. 플랫폼에 OpenBao를 새로 두고 Transit(ed25519)

**Pros**
* 키를 꺼낼 수 없고 정책이 세밀하다.

**Cons**
* 플랫폼에 운영 구성요소가 하나 는다. 봉인 해제(unseal) 키를 둘 곳이 다시 필요하다(결국 KMS).

### 4. AWS KMS Ed25519 키 + 플랫폼 ServiceAccount 웹 신원 (선택)

KMS는 `ECC_NIST_EDWARDS25519` 키로 순수 Ed25519(`ED25519_SHA_512`, RAW 메시지 4096바이트까지)를 서명한다. 표준 RFC 8032 서명이라 T19의 `Ed25519PublicKey.verify`가 그대로 받는다.

**Pros**
* 키를 꺼낼 수 없다(KMS 밖으로 나오지 않음). replica 수와 무관하다.
* 모든 서명이 CloudTrail에 남는다. 키 비활성화로 즉시 회수된다.
* 파드 신원은 장기 비밀 없이 projected ServiceAccount 토큰(audience `sts.amazonaws.com`)으로 받는다. ADR-0035가 온프렘 클러스터에 둔 공개 발급자 방식(`infra/envs/oidc-issuer`, #723)을 플랫폼 클러스터에도 쓴다. EKS의 IRSA와 같은 구조다.

**Cons**
* 플랫폼 kube-apiserver의 첫 발급자를 공개 주소로 바꿔야 한다. 기존 내부 발급자는 두 번째로 남긴다. 이 때문에 SA 토큰의 `iss`를 직접 검사하는 구성요소(Product Reader `reader.json`의 `issuer` 등)를 같은 창에서 맞춰야 한다.
* T19는 계획마다 한 공개키를 한 용도에만 받는다. 그래서 키가 6개다(월 6달러).

## 3. 결정 사항 (Decision Outcome)

* **4를 선택한다.** 용도 6개(context, run-source, run-target, readiness-binding, readiness-snapshot, gate-result)마다 KMS 키 하나를 둔다. 스택은 `infra/envs/platform-gate-signer`다.
  * 키 정책: 계정 관리자는 키를 관리하지만 서명하지 못한다(`NotAction kms:Sign`). 서명 역할만 `ED25519_SHA_512`·RAW로 서명한다. 그 밖의 주체가 서명하면 명시적으로 거부한다.
  * 서명 역할 신뢰: 플랫폼 발급자의 정확한 subject `system:serviceaccount:platform-state:t10-workflow-host`, audience `sts.amazonaws.com`. 와일드카드는 없다.
  * 경로: STS·KMS는 플랫폼 VPC interface endpoint로만 닿는다. workflow host NetworkPolicy는 그 ENI 사설 IP만 연다(`network.py --aws-endpoints`).
* **같은 검증기, 다른 발급자.** 플랫폼 키는 계획마다 `platform-<용도>` 발급자로 등록(enroll)한다. 경로는 운영자 키와 같은 relay 연산이다(`gate_signer`가 `gate_relay.handle`을 부른다). 운영자 키 경로(`operations/gate/run.py`)는 지우지 않는다. 검증기는 등록된 키 중 실제로 서명한 키를 받는다.
* **플랫폼 서명기의 주체는 운영자와 따로다.** `/workflow/gate-signer.json`의 relay 주체(admin·issuer·operator)에 별도 T10 권한을 준다. 권한과 기록을 운영자와 섞지 않는다.
* **위임 확인(GATE 연결):** 사람의 결정은 #716 사전 위임의 T19 전용 범위 `evidence`로 남는다. 이 범위는 `gate:publish-provenance`(직접 서명할 수 있는 발급자 권한)를 가진 사람만 기록할 수 있다. workflow host의 GATE 단계(`GateRecheck.sign`)는 적용 뒤 맥락을 묶은 다음, 평가하기 전에 서명한다. `sign`은 맥락을 읽은 직후, 아무것도 기록하거나 서명하기 전에 `ApprovalDelegations.principal_for(evidence)`로 계획·회수·만료·범위를 대조하고, 같은 버전의 다른 맥락이면 거부한다. 맞지 않으면 `delegation_unavailable`로 멈추고 서명은 0건이다. 확인된 위임 ID만 `platform-signature-*` 기록에 남는다. 운영자가 workflow host에서 직접 돌리는 `sign` CLI는 위임 없이 그대로 두고, 그때는 위임 ID를 기록하지 않는다(확인하지 않은 ID가 감사 원장 T24에 증거처럼 남지 않게). 서명 설정이 있는데 위임 서비스가 없으면 workflow host는 시작하지 않는다.
* **등록 시점:** T19는 키 등록보다 먼저 시작한 측정을 거부한다(`SUBJECT_PREDATES_AUTHORITY_ENROLLMENT`). 그래서 계획 시작 때, T14 실행보다 먼저 `enroll`한다. 등록 창은 48시간이다. 그보다 늦게 GATE에 닿은 계획은 HOLD한다(실패 쪽으로 닫힘).

## 4. 결과

* 코드: `services/platform-events/src/bravo_platform_events/gate_signer.py`(`enroll`·`sign`, CLI `python -m bravo_platform_events.gate_signer`), 시험 `services/control-gate/tests/test_gate_signer.py`(실제 T10 저장소에서 등록 → 기록·bind → 플랫폼 서명 → PASS, 잘못된 KMS 응답은 게시 전에 거부).
* 차트: `infra/charts/workflow-host` `gateSigner.roleArn`(비우면 AWS 신원 없음, 운영자 서명만).
* 이미지: `gate-records` 의존성 그룹에 boto3를 더한다.
* workflow host는 지금 replica 1이다(ApplicationHost 잠금이 단일 writer). 키 방식은 replica 수에 묶이지 않는다.
* **적용 순서(실환경, 이 ADR 병합 뒤):**
  1. `oidc-issuer`(#723)에 env `platform`을 더한다(플랫폼 `/openid/v1/jwks`).
  2. 플랫폼 kube-apiserver 첫 발급자를 그 주소로 바꾸고, 같은 창에서 내부 발급자를 검사하는 설정을 맞춘다.
  3. `platform-gate-signer`를 적용한다.
  4. NetworkPolicy에 endpoint IP를 넣는다.
  5. Argo 컨트롤러의 Deployment 정책(`registration-rbac.yaml`)에 `t10-workflow-host`의 projected `sts-token`(audience `sts.amazonaws.com`, `expirationSeconds` 3600 이하) 예외와 시험을 더한다. 지금 정책은 t15 `intake-token`만 허용해 `gateSigner.roleArn`을 넣으면 동기화가 거부된다.
  6. 이미지를 승격하고 `gateSigner.roleArn`·`gate-signer.json`·T10 권한을 넣는다.
  7. 계획마다 `enroll`한다. 이전 시간대에 이미 등록된 용도는 `reused`로 보고한다.
* 남은 일: GATE 단계에서 위임 확인 → `sign` → 평가를 자동으로 잇는 연결(적용 뒤 목적지 T14 실행 기록과 함께, df).
* **실제 서명 경계는 KMS 키 정책이 아니라 `platform-state` 권한이다.** workflow host는 relay admin(`gate:manage-provenance`)과 issuer를 함께 쥐고, T19는 등록된 키라면 어느 것이든 받는다. 그래서 `platform-state`의 `pods/exec` · `pods/create`(서명 SA를 지정한 Pod)와 T10 DB 쓰기 권한이 곧 서명 권한이다. 권한 세분화(T44)에서 함께 좁힌다. 후속: `platform-*` 발급자의 공개키를 설정(terraform output)으로 고정하고 다른 키 등록을 거부한다.
* **관리자 우회 경로와 감시 대상:** 계정 관리자는 `kms:PutKeyPolicy`로 자신에게 서명 권한을 줄 수 있다(잠김을 피하려고 남긴 경로). 서명 역할의 신뢰(`iam:UpdateAssumeRolePolicy`)와 OIDC 발급자 버킷의 `platform` JWKS 객체(`infra/envs/oidc-issuer`)도 신뢰 근거다. `PutKeyPolicy` · `ScheduleKeyDeletion` · `DisableKey` · `UpdateAssumeRolePolicy` · 그 JWKS 객체의 `PutObject`를 CloudTrail 경보 대상으로 둔다.
* 후속: 서명 역할의 KMS 문에 `aws:SourceVpce` 조건과 endpoint 정책을 더한다(지금은 exec로 읽은 projected 토큰으로 VPC 밖에서도 역할을 받을 수 있다). `platform-signature-*` 기록과 CloudTrail `kms:Sign` 이벤트(키 ARN · 시각 · 건수)를 T24 감사 원장에서 대조하는 방법은 T24와 함께 정한다.
