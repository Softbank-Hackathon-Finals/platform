# T17 Cloudflare 가중치 실행 어댑터

등록된 Cloudflare LB 하나의 가중치만 바꾼다. T10이 승인·현재 계획·단계·SLI 판정을 확인한 뒤 이 라이브러리를 호출한다. 외부 HTTP 서버, 자원 생성, LB 활성화, Tunnel 설치, DB 쓰기 전환은 포함하지 않는다.

## 소유와 연결

- T10: 현재 계획 잠금·사람 승인·권한·단계 진행, T36 창 수신 및 판정 저장. `authorize(command)`는 현재 명령의 승인과 순서를 다시 확인하고 정확히 `True`를 반환해야 한다. 명령 수명 동안 T10의 계획 잠금 또는 단독 실행 lease를 유지한다. 클라이언트 payload나 과거 승인만으로 `True`를 반환하지 않는다.
- T17: 신원과 설정을 확인한 LB에 PATCH 한 번, GET 재조회, 영속 영수증. 동일 LB에는 단일 쓰기 호스트와 단일 journal을 사용한다. 모든 실행 프로세스가 같은 영속 로컬 볼륨을 공유한다. 서로 다른 볼륨·복제본·관리자 콘솔의 동시 변경은 이 잠금으로 막을 수 없다.
- T36: 기존 `SliJudgement` 원문을 생산한다. 이 패키지는 판정 기준을 복제하거나 프로필을 변경하지 않는다. `bravo-observability`는 소스 커밋에 고정했다.
- T18: 실제 Edge를 통과한 요청 분배·오류율·p95 창을 생산한다. provider GET 성공과 실제 요청 분배를 구분한다.

```python
from pathlib import Path
from bravo_edge_control.provider import Binding, Cloudflare
from bravo_edge_control.control import Command, Controller, apply_sli

# 서버의 등록 자료에서 읽는다. API token은 전용 Secret에서 매 호출 다시 읽는다.
provider = Cloudflare(Binding(**registered_lb), token=read_cloudflare_token)
controller = Controller(provider, Path(persistent_journal), authorize=current_t10_authority)
try:
    # T10 계획 잠금 안에서 승인된 명령을 저장한 뒤 dispatch한다.
    receipt = controller.execute(Command(
        plan_id=plan_id, run_id=run_id, operation_id=durable_operation_id,
        expected_revision=0, expected_weight=0, weight_target=10,
    ))
    # T10이 현재 단계에 대해 저장한 canonical 판정만 전달한다.
    rollback = apply_sli(controller, stored_judgement,
        plan_id=plan_id, run_id=run_id, revision=receipt["revision"],
        weight=10, observe_from=server_stage_observe_from)
finally:
    provider.close()
```

이 예제의 변수·callback은 T10 호스트에서 제공한다. 배포 가능한 임의 입력 API로 사용하지 않는다. 10→50→100은 각 단계의 확인된 영수증과 `ADVANCE_ALLOWED` 판정 뒤에 T10이 새 명령을 만든다. 명령마다 현재 revision과 기대 가중치를 넣는다. `PASS`·`HOLD`·`WATCH` 판정은 `apply_sli`에서 provider 호출을 만들지 않는다. `SHIFT / SET_WEIGHT_0`만 실제 0% 명령으로 바꾸고 확인 영수증을 반환한다. 종료된 run을 새 멱등 키로 다시 올리는 요청은 거부한다. 새 실행은 새 run과 현재 T10 승인이 필요하다.

T10 `EdgePort.restore_origin(plan_id, command_id, execution_id, epoch)`에는 `FailbackEdge`를 연결한다. `resolve`는 해당 네 값에 결속된 영속 `Command`를 돌려준다. `operation_id`는 `failback_operation_id`로 만든다. reason은 `FAILBACK`, target은 0이다. 같은 인수는 같은 명령으로 조회한다. 현재 provider 재조회까지 성공한 경우만 `True`다. 이 어댑터는 DB Executor를 호출하지 않는다.

## 멱등성과 불명 상태

| 상태 | 의미와 다음 행동 |
|---|---|
| `CONFIRMED` | PATCH의 유효한 성공 응답 뒤 GET에서 같은 LB·설정·가중치를 확인했다. 실제 트래픽 반영 증명은 별도다. 같은 요청을 재호출하면 GET만 수행한다. |
| `AWAITING_READBACK` | 유효한 PATCH 응답은 받았지만 GET이 실패했거나 기대 가중치와 다르다. 같은 명령으로 GET만 재시도한다. 다른 명령은 막는다. |
| `UNCERTAIN` | intent 저장 뒤 프로세스 중단, PATCH timeout, 오류 또는 잘못된 응답이다. 같은 명령은 GET만 수행한다. GET이 기대값이어도 이 상태를 자동 해제하지 않는다. |
| `SUPERSEDED` | 이 명령 뒤 다른 명령이 기록됐다. 과거 영수증이며 현재 성공으로 쓰지 않는다. provider를 호출하지 않는다. |

SQLite `synchronous=FULL`로 intent를 먼저 commit하고 같은 journal의 OS 잠금 안에서 PATCH를 수행한다. 같은 키의 다른 내용, 오래된 revision, 다른 계획으로 재사용한 run, 잘못된 단계는 거부한다. SQLite DB와 `.lock`은 영속 볼륨에 함께 보존한다. journal을 지우거나 복제해 재시도하지 않는다.

Cloudflare PATCH에 원격 CAS나 이 라이브러리의 멱등 키 적용을 가정하지 않는다. 응답을 못 받은 요청이 늦게 도착할 수 있으므로 GET 한 번만으로 끝났다고 판정하거나 0% PATCH를 덧보내지 않는다. `UNCERTAIN`이면 실행을 중단하고 단일 운영 담당에게 인계한다. 지연 요청 종료·현재 provider 상태를 조사한 뒤 별도 복구 절차를 합의한다. 자동 해제 API는 없다. 운영 중 journal 손실이나 다른 writer 발견도 중단 사유다.

## 자원 조건과 기록

실행 전 LB가 활성·proxied·random steering·session affinity none이고 출발지/목적지 두 pool과 출발지 fallback으로 결속됐는지 확인한다. 다른 routing rule·`pool_sets`·지역 override가 있으면 거부한다. `pool_sets`는 이 PATCH에서 지워지지 않고 가중치와 다른 경로를 만들 수 있으므로 지원하지 않는다. 변경 전 조회·PATCH 응답·변경 후 조회에 같은 검사를 적용한다. 기존 비활성 자원은 실행 담당이 준비 조건과 작업창을 확인한 뒤 별도로 활성화한다. 어댑터는 그 설정을 변경하지 않는다.

영수증에는 plan/run/operation/revision, LB 참조, `request_at`, `response_at`, `confirmed_at`, 최근 `readback_at`, `readback_weights`, 상태·고정 오류 코드가 남는다. provider 응답 본문이나 token은 저장하지 않는다. API token은 고정 HTTPS Cloudflare origin으로만 보내고 redirect·환경 proxy·자동 재시도를 사용하지 않는다. journal의 운영 식별자는 비공개 자료로 보관한다. 공개 증빙은 저장소의 공개 증빙 절차로 만든다.

10/50/100/0 영수증의 `request_at`부터 첫 완전한 T18 유효창 끝까지를 관측 상한으로 대조한다. provider 설정 확인 시각을 실제 반영 시간으로 보고하지 않는다. SLI 실패로 중간에 0%로 돌아간 실행은 자동 복귀 시험으로 따로 남긴다. 완료된 4단계 측정으로 표시하지 않는다.

## 검증과 남은 통합

```sh
uv sync --frozen
uv run ruff check .
uv run pytest
```

시험은 합성 T18 창·canonical T36 judge와 HTTP 모의 provider를 사용한다. 실제 Cloudflare나 클러스터를 변경하지 않는다. T10 호스트의 현재 권한 callback·영속 명령 resolver와 연결하고, 기존 자원·인증·두 앱·실행창을 확인한 뒤 단일 실행 담당이 실제 측정을 수행해야 한다. 이 패키지의 시험 성공만으로 T17 이슈의 완료 항목을 체크하지 않는다.

원문: [팀 트래픽 설계](../../docs/design.html#s8-7), [T36 판정 계약](../../docs/observability/README.md), [T10 failback 포트](../platform-events/src/bravo_platform_events/failback.py), [Cloudflare PATCH API](https://developers.cloudflare.com/api/resources/load_balancers/methods/edit/).
