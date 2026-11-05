# [ADR-0027] 리허설 OpenBao 쓰기 identity: 인자로 역할을 묶고, 정책 본문의 잔여 위험은 기록한 뒤 후속에서 없앤다

* **상태 (Status):** 제안됨(Proposed). 로컬 kind·upstream OpenBao 2.7.2 dev에서 실측했다. 데모 목적지 RKE2 적용과 T15 실제 준비 Job 연결 뒤 채택한다.
* **날짜 (Date):** 2026-11-06
* **관련 (Related):** T9, T15 #24, T30, 설계 문서 8.4 "시험용 OpenBao 경로", [target-secrets README](../../infra/envs/target-secrets/README.md), [실측](../evidence/t9-openbao-writer-2026-11-06/README.md)

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

### 2. 경로 glob + 역할 인자 제한 (선택)

역할 경로에 `required_parameters`·`allowed_parameters`·`denied_parameters`를 둔다. SA·audience·TTL·default 정책 끔은 정확한 값, namespace는 `rehearsal-*`이되 `rehearsal-control*`와 쉼표·`*`·공백이 든 값은 거부, 정책은 `rehearsal-app-reader-*`.

**Pros**
* 쓰기 SA 혼자서는 상승할 수 없다(실측: 자기 SA·namespace 바인딩 거부, 그 역할 로그인 거부).
* 합의한 T15 흐름을 바꾸지 않는다.

**Cons**
* 정책 본문은 여전히 자유다. 쓰기 identity를 탈취하고 rehearsal namespace에서 `rehearsal-eso-reader` 토큰도 얻을 수 있으면 `bravo/target`을 읽는다(실측으로 확인).
* OpenBao 2.7.2는 read에도 `required_parameters`를 적용해 역할을 다시 읽을 수 없다.
* 목록 인자를 쓸 수 없어 reader 역할 본문을 문자열로 보내야 한다.

### 3. 운영자가 만든 고정 reader 정책 (고정 슬롯 또는 namespace 템플릿)

T9 운영자가 `rehearsal-app-reader-slot-<n>`을 미리 만들거나, Kubernetes 별칭 메타데이터 `service_account_namespace`로 경로를 정하는 템플릿 정책 하나를 만든다. 쓰기 identity에는 `sys/policies/acl` 권한을 주지 않는다.

**Pros**
* 정책 본문을 쓸 수 없으므로 위 상승 경로가 없어진다.

**Cons**
* KV 경로 계약(`<run>` → 슬롯 또는 namespace)이 바뀌어 T15 합의와 구현 변경이 필요하다.
* 이번에 시험하지 않았다.

## 3. 결정 사항 (Decision Outcome)

* **옵션 2를 지금 적용한다.**
* **옵션 3은 T15와 KV 경로 계약을 다시 합의해 후속으로 진행한다.**
* **옵션 1은 채택하지 않는다.**

**이유**
* 합의된 T15 흐름을 지금 열 수 있고, 쓰기 SA 단독 상승은 막힌다.
* 남는 위험을 "보안상 안전"으로 표시하지 않고, 실측과 운영 조건(짧은 TTL·폐기, reader 토큰 발급 권한 최소화, 정책 쓰기 감사)으로 드러낸다.
* 동등성은 시험으로 판단한다는 원칙에 따라 허용·거부·상승을 실제 서버에서 확인했다.

## 4. 결과

* 로컬 실측 96개 사례가 기대대로였다. 권한 상승 사례는 **성공**(위험 실재)으로 기록했다.
* 목적지 RKE2·TLS·팀 이미지에서는 아직 적용·시험하지 않았다.
* OpenBao `network` 정책은 `rehearsal-control`의 `app.kubernetes.io/component=rehearsal-openbao-writer` Pod만 추가로 받는다. rehearsal 시도 namespace의 reader Pod 수신 규칙은 없고, 이번 변경에서 추가하지 않았다.
