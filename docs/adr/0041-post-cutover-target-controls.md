# [ADR-0041] 컷오버 뒤 목적지 T14 run을 플랫폼 신원이 수집한다

* **상태 (Status):** 제안됨(Proposed). 2026-11-07 사용자 결정. T30·T9 담당 리뷰 동의 대기
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T10 WATCH(ADR-0031), T14·T19 재시험(`watch_retest.py`), #666 리뷰 2번, #748

## 1. 배경 및 문제 상황

* **상황:** WATCH는 T36 SLI PASS와 T19 재시험 PASS가 모두 있어야 DONE이 된다. 재시험은 "컷오버 뒤에 시작한 목적지 T14 run"을 GATE 맥락(`apply-recheck:1` = SEC-01.1·IAM-01.1·ENC-01.1)으로 출발지 run과 비교한다(`watch_retest.py`).
* **문제:** 그 run을 만드는 곳이 운영자 도구(`operations/controls/run.py`, 운영자 Mac의 `kubectl create token` + port-forward)뿐이다. 그래서 무인 해피패스에서는 WATCH가 늘 UNKNOWN에서 멈춘다. 실서버는 이 자리에 항상 UNKNOWN을 내는 가짜 재시험(`pending_retest`)까지 연결돼 있었다(#748에서 삭제).
* **제약:** #666 리뷰 2번에 따라 클러스터 안에는 SA 토큰을 만드는 권한을 두지 않는다. RBAC는 TokenRequest의 audience와 수명을 제한하지 못하므로, 그런 권한은 앱 OpenBao 역할로 가는 두 번째 길이 된다.
* **목표:** 사람 없이 컷오버 뒤 목적지 run을 기록하되, 클러스터 안에 발급 권한을 만들지 않는다.

## 2. 고려한 옵션들

### 1. 플랫폼 신원이 클러스터 밖에서 수집 (선택)

workflow host가 플랫폼의 짧은 수명 목적지 신원(kubeconfig 하나)으로 운영자 도구와 같은 일을 한다. 클러스터 밖에서 TokenRequest를 하고(audience `openbao`, 600초, 코드에서 고정), 목적지 API의 port-forward로 OpenBao·SeaweedFS에 닿는다. 등록된 T14 수집기(`bravo_controls`)로 GATE 맥락 프로필의 run을 만들고, `GateRecords.record_run(side=target)`으로 기록한다.

**Pros**
* 클러스터 안에 발급 권한도, 측정 Pod도 없다. 목적지에 이미지·NetworkPolicy를 더하지 않는다.
* 운영자 도구와 측정 방식이 같다. 같은 수집기, 같은 증거, 같은 판정을 쓴다.
* 권한을 사람이 아니라 플랫폼이 쥔다(11/7 운영 방향).

**Cons**
* 플랫폼 신원이 두 SA의 토큰을 만들 수 있다. 오늘 운영자(cluster-admin)가 가진 능력을 플랫폼도 갖게 된다. 그래서 RBAC를 이름으로 좁히고(아래), 토큰의 audience와 수명을 코드에서 고정한다.

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
* **ENC-01.1(`pooler-tls`)은 이 PR에 넣지 않는다.** 이 수집기는 앱 DB 비밀번호(SCRAM 로그인)와 psql 18이 필요하다. 플랫폼 신원에 앱 DB Secret 읽기를 줄지, 접속 전용 DB 역할을 둘지 정한 뒤 붙인다. 그때까지 `apply-recheck:1` 맥락의 재시험은 ENC-01.1이 NOT_RUN이므로 UNKNOWN이다. 가짜 PASS를 만들지 않는다.

**목적지 쪽 플랫폼 신원의 권한(이름으로 한정)**

| namespace | 자원 | 동사 | 이유 |
|---|---|---|---|
| `target-dev` | `serviceaccounts/token` (`demo-chat`, `t14-other`) | create | SEC-01.1 두 신원 |
| `target-dev` | `secrets` (`target-object-client`) | get | IAM-01.1 앱 client 키 |
| `target-dev`, `bravo-secrets` | `services` (`seaweedfs`, `target-openbao`) | get | port-forward 대상 해석 |
| `target-dev`, `bravo-secrets` | `pods` | list, get | port-forward 대상 Pod |
| `target-dev`, `bravo-secrets` | `pods/portforward` | create | 클러스터 안 서비스 접근 |

**이유**
* #666 리뷰가 막으려던 것은 클러스터 안의 발급자다. 이 결정은 그 경계를 지킨다. 발급자는 클러스터 밖의 플랫폼 하나이고, 요청 내용은 코드가 고정한다.
* 재시험은 실제 수집 결과로만 PASS가 된다. 수집하지 못한 시험은 NOT_RUN → UNKNOWN이다.

## 4. 결과

* 실서버 롤아웃과 실측은 아직이다. 목적지 RBAC 적용, 플랫폼 kubeconfig, workflow host 이미지(`bravo_controls` 포함)가 남았다. 결과는 이 절에 기록한다.
