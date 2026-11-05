# [ADR-0029] 리허설 OpenBao 쓰기 identity: 정책 쓰기 권한 없이, 운영자 소유 namespace 템플릿 정책에 역할만 묶는다

* **상태 (Status):** 제안됨(Proposed). 11/6 원결정(옵션 2)은 같은 날 개정(옵션 3b, "A안")으로 **대체됨**(5절). 두 설계 모두 로컬 kind·upstream OpenBao 2.7.2 dev에서 실측했다. 데모 목적지 RKE2 적용과 T15 실제 준비 Job 연결 뒤 채택한다.
* **날짜 (Date):** 2026-11-06 (원결정), 2026-11-06 (A안 개정)
* **관련 (Related):** T9, T15 #24, T30, 설계 문서 8.4 "시험용 OpenBao 경로", [target-secrets README](../../infra/envs/target-secrets/README.md), [실측](../evidence/t9-openbao-writer-2026-11-06/README.md)

1~4절은 원결정 당시의 기록이다(당시 관리 namespace 이름 `rehearsal-control` 그대로). 결정은 5절이 대체한다.
브랜치 초안에서는 0027번이었으나 develop의 `0027-plan-evaluation-profile`과 겹쳐 0029로 옮겼다.

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** T15 러너는 시도마다 합성 자격을 `bravo/rehearsal/<run>/database`에 쓰고, 그 실행만 읽는 reader 정책 `rehearsal-app-reader-<run>`과 역할을 만든 뒤 지운다. 11/6 01:50 Slack에서 T15 담당과 준비 Job을 `rehearsal-control` namespace의 SA `rehearsal-openbao-writer`(audience `openbao`)로 돌리기로 합의했다.
* **문제:** 정책을 쓸 수 있고 역할을 묶을 수 있는 identity는 자기 이름 범위 안의 정책에 아무 본문이나 넣을 수 있다. OpenBao ACL은 경로와 요청 인자만 보고 정책 본문을 검사하지 못한다.
* **목표:** 쓰기 identity를 합의한 경로로만 제한하고, 막을 수 없는 부분은 시험으로 확인해 숨기지 않는다.

## 2. 고려한 옵션들 (Considered Options)

### 1. 경로 glob만 제한

`bravo/rehearsal/*`, `sys/policies/acl/rehearsal-app-reader-*`, `auth/kubernetes/role/rehearsal-app-reader-*`만 허용한다.

**Pros**
* 단순하다.

**Cons**
* 역할 인자가 자유라 쓰기 SA 자신·`target-dev`·`*` namespace에 정책을 묶어 바로 `bravo/target`을 읽을 수 있다.

### 2. 경로 glob + 역할 인자 제한

역할 경로에 `required_parameters`·`allowed_parameters`·`denied_parameters`를 둔다. SA·audience·TTL·default 정책 끔은 정확한 값, namespace는 `rehearsal-*`이되 `rehearsal-control*`와 쉼표·`*`·공백이 든 값은 거부, 정책은 `rehearsal-app-reader-*`.

**Pros**
* 쓰기 SA 혼자서는 상승할 수 없다(실측: 자기 SA·namespace 바인딩 거부, 그 역할 로그인 거부).
* 합의한 T15 흐름을 바꾸지 않는다.

**Cons**
* 정책 본문은 여전히 자유다. 쓰기 identity를 탈취하고 rehearsal namespace에서 `rehearsal-eso-reader` 토큰도 얻을 수 있으면 `bravo/target`을 읽는다(실측으로 확인).
* OpenBao 2.7.2는 read에도 `required_parameters`를 적용해 역할을 다시 읽을 수 없다.
* 목록 인자를 쓸 수 없어 reader 역할 본문을 문자열로 보내야 한다.

### 3. 운영자가 만든 고정 reader 정책 (고정 슬롯 또는 namespace 템플릿)

T9 운영자가 (a) `rehearsal-app-reader-slot-<n>`을 미리 만들거나, (b) Kubernetes 별칭 메타데이터 `service_account_namespace`로 경로를 정하는 템플릿 정책 하나를 만든다. 쓰기 identity에는 `sys/policies/acl` 권한을 주지 않는다.

**Pros**
* 정책 본문을 쓸 수 없으므로 위 상승 경로가 없어진다.

**Cons**
* KV 경로 계약(`<run>` → 슬롯 또는 namespace)이 바뀌어 T15 합의와 구현 변경이 필요하다.
* 원결정 시점에는 시험하지 않았다.

## 3. 결정 사항 (Decision Outcome) — 원결정, 5절로 대체됨

* **옵션 2를 지금 적용한다.**
* **옵션 3은 T15와 KV 경로 계약을 다시 합의해 후속으로 진행한다.**
* **옵션 1은 채택하지 않는다.**

**이유**
* 합의된 T15 흐름을 지금 열 수 있고, 쓰기 SA 단독 상승은 막힌다.
* 남는 위험을 "보안상 안전"으로 표시하지 않고, 실측과 운영 조건(짧은 TTL·폐기, reader 토큰 발급 권한 최소화, 정책 쓰기 감사)으로 드러낸다.
* 동등성은 시험으로 판단한다는 원칙에 따라 허용·거부·상승을 실제 서버에서 확인했다.

## 4. 결과 — 원결정

* 로컬 실측 96개 사례가 기대대로였다. 권한 상승 사례는 **성공**(위험 실재)으로 기록했다.
* 목적지 RKE2·TLS·팀 이미지에서는 아직 적용·시험하지 않았다.
* OpenBao `network` 정책은 `rehearsal-control`의 `app.kubernetes.io/component=rehearsal-openbao-writer` Pod만 추가로 받는다. rehearsal 시도 namespace의 reader Pod 수신 규칙은 없고, 이번 변경에서 추가하지 않았다.

## 5. 2026-11-06 개정: 옵션 3(b) "A안" 채택

### 배경

원결정의 상승 경로(쓰기 토큰으로 `rehearsal-app-reader-evil` 정책에 `secret/data/bravo/target/*` 읽기를 넣고, 모든 인자 제한을 지키는 역할로 `rehearsal-<id>`의 `rehearsal-eso-reader`에 묶으면 그 SA 토큰이 운영 값을 읽음)가 OpenBao 2.7.2에서 재현됐다. 사용자는 쓰기 identity의 정책 생성·수정 능력을 아예 없애는 A안을 결정했다.

### 결정

* **쓰기 identity에는 `sys/policies/*` 경로를 하나도 주지 않는다.** 정책 `rehearsal-openbao-writer`는 `bravo/rehearsal/*` KV data·metadata의 create/read/update/delete, `auth/kubernetes/role/rehearsal-app-reader-*`의 create/update/delete, 자기 토큰 조회·갱신·폐기만 갖는다.
* **reader 정책은 운영자 소유 `rehearsal-app-reader` 하나다.** 설치 때 운영자가 `auth enable kubernetes` 뒤 mount accessor를 읽어(`bao read -field=accessor sys/auth/kubernetes`) `rehearsal-reader-policy --kubernetes-accessor <accessor>`로 렌더해 한 번 쓴다. 경로는 `secret/data/bravo/rehearsal/{{identity.entity.aliases.<accessor>.metadata.service_account_namespace}}/*` 읽기다.
* **역할 본문의 `token_policies`는 `rehearsal-app-reader`와 정확히 같아야 한다**(허용값에 `*`가 없어 동등 비교). 나머지 제한(SA `rehearsal-eso-reader`, audience `openbao`, TTL 300/900초, default 정책 끔, namespace `rehearsal-*`이되 `rehearsal-control*`·쉼표·`*`·공백 거부, 필수 인자 일곱 개)은 그대로다.
* **KV 경로와 역할 이름을 namespace 기준으로 바꾼다.** `bravo/rehearsal/<namespace>/database`, 역할 `rehearsal-app-reader-<namespace>`. 렌더러의 `rehearsal-auth-role`·`rehearsal-delivery`·`rehearsal-init-container`는 `--namespace`만 받고, 실행별 `rehearsal-policy` 단계와 `--run-id`는 없앤다. 템플릿이 namespace로 범위를 정하므로 실행 id를 따로 두면 두 키를 맞춰야 하는 부담만 생긴다. 시도마다 namespace가 새로 생기므로 실행 단위 격리는 유지된다.
* **관리 namespace를 `bravo-rehearsal-control`로 바꾼다**(T15 담당 요청, 11/6 03:19 Slack, platform-events 설정과 같은 이름). SA `rehearsal-openbao-writer`와 audience `openbao`는 그대로다. 쓰기 역할 바인딩, OpenBao `network` 정책의 쓰기 peer, 선택 identity 매니페스트가 이 이름을 쓴다. 새 이름은 `rehearsal-*`에 맞지 않아 reader namespace가 될 수 없다. 옛 이름 `rehearsal-control*`는 `rehearsal-*`에 맞으므로 렌더러와 쓰기 ACL에서 계속 reader namespace로 거부한다. 이 namespace는 platform-events의 rehearsal host(`rehearsal-host`·`rehearsal-exec`)와 함께 쓰므로, 그곳에서 `rehearsal-openbao-writer`로 Pod를 만들거나 토큰을 발급할 수 있는 identity는 T15 러너로 한정해야 한다.
* 고정 슬롯(옵션 3a)은 슬롯 수 관리와 슬롯 재사용 정리가 필요해 택하지 않는다.

### 근거 (소스)

OpenBao 2.7.2(커밋 `9e0a0d3`)의 내장 Kubernetes 인증(`internal/builtin/credential/kubernetes/path_login.go`)은 TokenReview로 JWT의 이름·UID·namespace가 맞는지 확인한 뒤, 별칭 메타데이터에 `service_account_namespace`를 넣는다. ACL 경로 템플릿 `identity.entity.aliases.<mount accessor>.metadata.<key>`는 `sdk/helper/identitytpl`이 풀고, 풀리지 않으면 `internal/vault/policy/policy.go`가 그 규칙을 건너뛴다. 따라서 accessor가 틀리거나 바뀌면 reader는 거부된다(넓어지지 않는다).

### 결과

* 로컬 kind(`t9-aplan`)·upstream OpenBao 2.7.2 dev에서 157개 사례가 모두 기대대로였다([실측](../evidence/t9-openbao-writer-2026-11-06/README.md#a안-재실측)). 이전 상승 경로의 각 단계(정책 쓰기, 다른 정책 바인딩, 그 역할 로그인)는 모두 **거부**됐다. `rehearsal-aaa` reader는 자기 경로만 읽고 `rehearsal-bbb`·접두 형제·`bravo/target`은 거부됐다. 렌더된 init container가 `credentials.json`을 만들고 정리 Job이 역할과 값을 지웠다.
* **남는 위험:** 쓰기 토큰을 탈취하면 모든 rehearsal namespace의 합성 값을 읽고 바꾸고 지울 수 있고, `rehearsal-*`(`rehearsal-control*` 제외) 어디에든 reader 역할을 만들거나 다시 묶을 수 있다. 그래도 그 토큰은 자기 namespace 경로(쓰기 identity가 이미 읽을 수 있는 값)만 읽으므로 운영 값으로 가는 경로는 없다. 다른 namespace의 역할을 덮어쓰면 서비스 거부가 된다. reader 정책의 무결성은 운영자와 올바른 accessor에 달려 있다. 운영자 정책 쓰기 감사, 쓰기 토큰의 짧은 TTL·폐기, reader 토큰 발급 권한 최소화는 계속 유지한다.
* **T15 계약 변경:** KV 경로 `bravo/rehearsal/<namespace>/database`(키 `credentials_json`, CAS 0), 역할 이름 `rehearsal-app-reader-<namespace>`, 역할 본문 `rehearsal-auth-role --namespace <namespace>` 출력(`token_policies: "rehearsal-app-reader"`). 준비 Job은 정책을 만들거나 지우지 않는다.
* 목적지 RKE2·TLS·팀 이미지와 T15 실제 Job에서는 아직 적용·시험하지 않았다. rehearsal 시도 namespace의 reader 네트워크 peer는 여전히 렌더 결과에 없다(실측에서는 시험 전용 peer로 대신했다).
