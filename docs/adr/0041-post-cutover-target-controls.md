# [ADR-0041] 컷오버 뒤 목적지 T14 run을 플랫폼 신원이 수집한다

* **상태 (Status):** 제안됨(Proposed). 2026-11-07 사용자 결정. T14 동의(#765 승인), T9 동의(#936 리뷰, 2026-11-08). T30 담당 리뷰 동의 대기
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T10 WATCH(ADR-0031), T14·T19 재시험(`watch_retest.py`), #666 리뷰 2번, #748

## 1. 배경 및 문제 상황

* **상황:** WATCH는 T36 SLI PASS와 T19 재시험 PASS가 모두 있어야 DONE이 된다. 재시험은 "컷오버 뒤에 시작한 목적지 T14 run"을 GATE 맥락(`apply-recheck:1` = SEC-01.1·IAM-01.1·ENC-01.1)으로 출발지 run과 비교한다(`watch_retest.py`).
* **문제:** 그 run을 만드는 곳이 운영자 도구(`operations/controls/run.py`, 운영자 Mac의 `kubectl create token` + port-forward)뿐이다. 그래서 무인 해피패스에서는 WATCH가 늘 UNKNOWN에서 멈춘다. 실서버는 이 자리에 항상 UNKNOWN을 내는 가짜 재시험(`pending_retest`)까지 연결돼 있었다(#748에서 삭제).
* **제약:** #666 리뷰 2번이 짚은 것은 위치가 아니라 **능력**이다. RBAC는 TokenRequest의 audience와 `expirationSeconds`를 제한하지 못한다. 그래서 `demo-chat` 토큰을 만들 수 있는 주체는 원하는 audience와 수명으로 그 토큰을 만들 수 있고, 그 토큰은 앱 OpenBao 역할로 가는 두 번째 길이 된다. 목적지 API 서버에는 `service-account-max-token-expiration`이 없어 클러스터가 상한을 두지 않는다(11/8 확인).
* **목표:** 사람 없이 컷오버 뒤 목적지 run을 기록한다. 발급 능력은 클러스터 안의 워크로드에 주지 않는다. 플랫폼 신원의 발급은 RBAC가 아니라 **admission이 audience와 수명을 강제**하게 한다.

## 2. 고려한 옵션들

### 1. 플랫폼 신원이 클러스터 밖에서 수집 (선택)

workflow host가 플랫폼의 짧은 수명 목적지 신원(kubeconfig 하나)으로 운영자 도구와 같은 일을 한다. 클러스터 밖에서 TokenRequest를 하고(audience `openbao`, 600초, 코드에서 고정), 목적지 API의 port-forward로 OpenBao·SeaweedFS에 닿는다. 등록된 T14 수집기(`bravo_controls`)로 GATE 맥락 프로필의 run을 만들고, `GateRecords.record_run(side=target)`으로 기록한다.

**Pros**
* 클러스터 안에 발급 권한도, 측정 Pod도 없다. 목적지에 이미지·NetworkPolicy를 더하지 않는다.
* 운영자 도구와 측정 방식이 같다. 같은 수집기, 같은 증거, 같은 판정을 쓴다.
* 권한을 사람이 아니라 플랫폼이 쥔다(11/7 운영 방향).

**Cons**
* 플랫폼 신원이 두 SA의 토큰을 만들 수 있다. 오늘 운영자(cluster-admin)가 가진 능력을 플랫폼도 갖게 된다. 그래서 RBAC를 이름으로 좁히고, audience `openbao`와 600초를 **admission(VAP)이 이 신원에 한해 강제**한다(아래). 코드의 고정값만 믿지 않는다.

### 2. 목적지 안 Pod 두 개가 각자 projected 토큰으로 수집

SA `demo-chat`과 `t14-other`로 Pod를 하나씩 돌린다. 각 Pod는 자기 projected 토큰(audience `openbao`)으로 로그인한다.

**Cons**
* Pod를 만드는 권한이 곧 그 SA로 실행하는 권한이다. 결국 발급 권한과 같은 범위가 된다.
* 목적지에 arm64 수집 이미지와 NetworkPolicy가 더 필요하다. 두 Pod의 결과를 한 run으로 묶는 단계도 새로 만들어야 한다.

### 3. 운영자가 계속 기록

**Cons**
* WATCH가 사람 한 번에 묶인다. 무인 해피패스 목표와 맞지 않는다.

## 3. 결정 사항

* **옵션 1을 선택한다.** 생산자는 `bravo_platform_events.target_controls`이고, `ports.json`의 `watch.target_controls`로 켠다. 켜지 않으면 WATCH는 지금처럼 운영자의 기록을 기다린다.
* **옵션 2는 채택하지 않는다.** 권한 범위가 같거나 더 넓다.
* **ENC-01.1(`pooler-tls`, #758 등록 규약)도 잰다.** `target-pgbouncer`로 port-forward한 loopback에서 verify-full로 접속한다. 증거의 client는 `port-forward`로 남는다. SCRAM 로그인에 쓰는 앱 DB 자격은 SEC-01.1과 같은 짧은 수명 TokenRequest로 받은 **앱 자신의 OpenBao 로그인**으로 앱 경로(`credentials_json`)에서 읽는다. 메모리에만 두고, Secret 읽기나 DB 관리자 권한은 쓰지 않는다. psql은 이미지에 고정한 PGDG `postgresql-client-18`을 쓴다. 그 sha256과 버전은 수집할 때 이미지 안 바이너리에서 읽고, 이미지 digest가 바이너리를 고정한다.
* **AUD-01.1(`pod-log`)은 넣지 않는다.** CNPG primary에 postgres로 exec하는 것은 DB 관리자 접속이다. 재대조·재시험 프로필은 `apply-recheck:1`(SEC·IAM·ENC)이라 필요하지 않다.

**목적지 쪽 플랫폼 신원의 권한(이름으로 한정)**

| namespace | 자원 | 동사 | 이유 |
|---|---|---|---|
| `target-dev` | `serviceaccounts/token` (`demo-chat`, `t14-other`) | create | SEC-01.1 두 신원 |
| `target-dev` | `secrets` (`target-object-client`) | get | IAM-01.1 앱 client 키 |
| `target-dev`, `bravo-secrets` | `services` (`seaweedfs`, `target-pgbouncer`, `target-openbao`) | get | port-forward 대상 해석 |
| `target-dev`, `bravo-secrets` | `pods` | list, get | port-forward 대상 Pod |
| `target-dev`, `bravo-secrets` | `pods/portforward` | create | 클러스터 안 서비스 접근. `bravo-secrets`는 `target-openbao-0`만. `target-dev`는 RBAC로 좁히지 못하므로 admission이 Pod를 고른다(아래) |

**RBAC가 못 막는 것은 admission이 막는다** (`t14_gate.py collector`, 권한보다 먼저 설치, `Deny`)

| 정책 | 대상 요청 | 허용 |
|---|---|---|
| `bravo-t14-collector-token` | 이 신원의 `serviceaccounts/token` CREATE | audience가 정확히 `["openbao"]`이고 `expirationSeconds == 600`인 요청만. 긴 수명이나 다른 audience의 `demo-chat` 토큰은 거부된다 |
| `bravo-t14-collector-forward` | 이 신원의 `pods/portforward` CONNECT | `target-dev`의 `target-seaweedfs-*`·`target-pgbouncer-*`, `bravo-secrets`의 `target-openbao-0`만. DB Pod(`target-db-*`)와 앱 Pod로는 닿지 않는다 |

**수집기 자신의 자격(발급 · 갱신 · 회수)**

| 항목 | 규칙 |
|---|---|
| 발급 | 목적지 클러스터 관리자가 `kubectl create token bravo-t14-collector -n bravo-apply-access --duration=24h`로 만든다. 플랫폼 Secret의 `kubeconfig`(JSON, 사용자 하나, server는 workflow host의 목적지 경로)에 넣는다. 레거시 SA Secret 토큰(만료 없음)은 쓰지 않는다 |
| 검사 | 수집할 때마다 `bounded_credential`로 확인한다. `exp`가 없거나, `exp - iat`가 24시간을 넘거나, 남은 시간이 600초보다 짧으면 `collector_credential_unbounded`로 거부한다. 이때 WATCH는 UNKNOWN에서 기다린다 |
| 갱신 | 플랫폼 운영 담당이 24시간마다 새 토큰으로 Secret을 바꾼다. 다음 수집이 새 파일을 읽는다. 데모 기간에는 운영 세션이 맡는다. 장기적으로는 T31 `applyCredential`처럼 exec 플러그인(`credentials.acquire`, 600초)으로 옮긴다 |
| 회수 | 목적지의 RoleBinding 두 개를 지우면 바로 막힌다. 플랫폼 Secret을 지우면 남은 토큰도 쓰이지 않는다. 이미 발급된 SA 토큰은 수명(최대 24시간)이 지나면 끝난다. 유출이 의심되면 **앱 DB 비밀번호도 교체**한다(아래 남는 위험) |

**이유**
* #666 리뷰가 막으려던 능력(긴 수명·임의 audience의 앱 토큰)은 admission으로 이 신원에서 없앤다. 발급자는 클러스터 밖의 플랫폼 하나이고, 클러스터 안의 워크로드에는 발급 권한이 없다.
* 재시험은 실제 수집 결과로만 PASS가 된다. 수집하지 못한 시험은 NOT_RUN → UNKNOWN이다.

**플랫폼 쪽 권한:** 기록 주체 `workflow-host`에 계획별 `gate:record`가 필요하다. 주는 방법과 주의는 [운영 사전 점검 · 권한](../operations/happy-path-preflight.md#권한-operatoraccess)을 따른다.

**남는 위험 (받아들인 범위, 사용자 결정 11/7)**
* **플랫폼 신원 = 앱 DB 로그인 능력.** `platform-state`의 kubeconfig Secret을 읽거나 workflow host에 exec할 수 있는 주체는 600초짜리 `demo-chat` 토큰을 되풀이해 만들 수 있다. 그 토큰으로 OpenBao `demo-chat` 역할(`demo-chat-db-reader`)에서 앱 DB 자격을 읽고, pooler로 앱 권한의 DB 로그인을 할 수 있다. ENC-01.1이 쓰는 설계상 능력이다. 회수는 RoleBinding 삭제, Secret 삭제, 앱 DB 비밀번호 교체 순서다.
* **SeaweedFS Pod의 loopback 포트.** port-forward는 Pod network namespace의 `127.0.0.1`로 붙으므로, `target-seaweedfs-*`의 loopback 전용 master·volume·filer에도 닿는다. 그러면 S3 인증 없이 filer로 객체를 읽고 쓸 수 있다. admission은 CONNECT의 포트를 보지 못한다. 다음 단계에서 T44 `tailnet-ingress`로 S3 8333만 플랫폼 태그에 열고, `target-dev` port-forward를 pgbouncer만 남긴다.
* **감사.** 목적지 감사 정책은 Metadata 수준이라, 이 신원의 TokenRequest는 누가·언제만 남고 본문(audience·수명)은 남지 않는다. 수명과 audience는 admission이 강제해서 어긋난 요청은 거부되고, 거부도 감사에 남는다. 본문 수준 감사(`Request`)는 노드 감사 정책 변경(#645 모듈)으로 다음에 더한다.
* **기록 주체.** run 영수증에는 기록한 주체가 없다. workflow-host의 `gate:record`는 계획별로 주고, 목적지 쪽 run만 기록한다. #740의 플랫폼 서명과 합칠 때 `recorded_by`를 영수증에 넣는다.
* **측정 대상 결속.** 생산자는 GATE 맥락의 `target_environment_ref`가 등록 ref와 다르면 측정하지 않는다(`target_environment_ref_mismatch`). 수집기 kubeconfig의 주체가 `bravo-t14-collector`가 아니면 거부한다(`collector_credential_subject`).

## 4. 결과

* 실서버 롤아웃과 실측은 아직이다. 목적지 RBAC 적용, 플랫폼 kubeconfig, workflow host 이미지(`bravo_controls` 포함)가 남았다. 결과는 이 절에 기록한다.
