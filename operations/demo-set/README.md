# 데모 세트: 새 계획 하나로 해피패스 실행

데모 플레이(AWS EKS → 온프렘 RKE2)를 새 계획으로 다시 돌릴 때 쓰는 운영자 도구 묶음이다. 매번 세션 기록에서 명령을 꺼내 조립하지 않도록 순서를 고정한다.

## 준비

1. `demo.example.json`을 복사해 `~/.bravo-demo/demo.json`을 만들고 값을 채운다. 이 파일은 커밋하지 않는다. 계정·역할·버킷 같은 운영 식별자와 키 경로는 이 파일에만 둔다.
   - 다른 위치에 두려면 `BRAVO_DEMO_CONFIG`로 지정한다.
2. 모든 cluster 호출은 `clusters.<side>.uid`(kube-system UID)를 먼저 대조한다. 다르면 아무것도 바꾸지 않고 멈춘다.
3. 운영자 SSO에 로그인한다(`aws sso login --profile <aws_profile>`).

## 데모 모드 (5분 플레이)

플랫폼 설정에서 아래 두 값을 켜면 REHEARSE·GATE·WATCH가 검사 없이 바로 지나간다(#1027, #1029).

- workflow host `ports.json`: `"demo": {"skip": ["REHEARSE", "GATE", "WATCH"]}`
- 승인 호스트 `operator.json`: `"demo": true`
  - 컷오버와 SHIFT가 T19 세션 없이 진행된다.
- 승인 호스트 `operator.json` `delegation.auto_approver`: 계획마다 위임을 자동으로 기록한다(#1009).
  - 사람이 승인하는 단계가 없다.
- 승인 호스트 `security_review: false`
  - 보안 검토 등록 단계가 없다.

데모 모드에서는 T15 리허설, 변경 승인, T31 적용, T19 재평가, WATCH 재시험을 하지 않는다. 그래서 운영 판정 근거로 쓰지 않는다.

## 순서

모든 명령은 platform checkout 최상위에서 실행한다.

| 단계 | 명령 | 결과 |
|---|---|---|
| 0. 수집기 토큰 | `python3 operations/demo-set/refresh_collectors.py <새 Secret 이름>` | 등록 호스트 수집기 토큰(8시간). 출력의 values 조각으로 bravo-gitops `registration.runtime.secrets.collectors`를 바꾼다. 토큰이 만료되면 등록이 `collector_denied`(503)로 거부된다. |
| 1. 계획 등록 | `cd services/platform-events && uv run python ../../operations/demo-set/register_plan.py <멱등 키>` | plan_id |
| 2a. T16 터널 | `python3 operations/demo-set/connect.py --profile <aws_profile> --port 18516` (별도 터미널) | 읽기 전용 수집 경로 |
| 2b. T16 수집 | `python3 operations/demo-set/inventory_reader.py scan --config <t16.reader_config> --profile <aws_profile> --port 18516 --output-dir <workdir>/<plan>/collection-01 --repo <t16.repo_checkout> --platform <t16.platform_checkout>` | `collection-01/` |
| 2c. 권한 | `python3 operations/demo-set/grants.py <plan> --apply` | 계획 권한(12시간) |
| 2d. T16 접수 | `python3 operations/demo-set/t16_accept.py <plan> collection-01` | COLLECT → ANALYZE |
| 3. S1 | `python3 operations/demo-set/s1.py <plan>` | T19 키 등록, T14 출발지·목적지, 서명, 평가. 데모 계획은 열린 버킷 때문에 GATE BLOCK(IAM-01.1)이 정상이다. 이어서 ANALYZE → PROPOSE. |
| 4. catalog | `python3 operations/demo-set/catalog_task.py <plan>` | 리허설 task(서명된 catalog) |
| 5. 자동 진행 | workflow host `propose.auto.plans`에 plan_id를 넣어 반영 | PROPOSE(AI·후보) → REHEARSE·GATE(데모 통과) → AWAIT_APPROVAL(자동 위임) → SHIFT(Edge) → CUTOVER → WATCH(데모 통과) → DONE |

각 단계 결과는 `<workdir>/<plan-id>/`에 남는다. 같은 키나 같은 계획으로 다시 실행해도 기존 기록을 읽는다.

## 데모 플레이 (CI·이미지 발행·helm 없이, 실측 1분 37초)

11/8 13:05:26 → 13:07:03(plan-fd7b498d…): COLLECT → … → AWAIT_APPROVAL 3초, 자동 SHIFT 승인 8초, SHIFT 0→100% 3초, 컷오버 72초(Executor 9단계 DONE), WATCH → DONE 1초.

1. `register_plan.py <키>` → `grants.py <plan> --apply`
2. 두 명령을 함께 실행한다.
   - `kubectl -n platform-state exec -i deploy/t10-workflow-host -- python - <plan> <이미지 digest> < operations/demo-set/drive.py`
     - COLLECT~GATE·WATCH를 이 계획만 즉시 넘긴다.
     - 이미지는 목적지에서 이미 동작하는 것을 쓴다.
   - `kubectl -n platform-state exec -i deploy/t15-rehearsal-host -- python - <plan> [epoch] < operations/demo-set/shift_cutover.py`
     - SHIFT에 들어오면 판정을 PASS로 기록해 100%까지 올린다.
     - 이어서 위임 컷오버를 제출한다.

실행 전 확인할 것(이번 실행에서 막혔던 곳):
- **앱 쓰기 차단:** 양쪽 demo-chat의 런타임 쓰기 차단이 꺼져 있어야 한다. `WRITE_BLOCKED=true`로 뜬 Pod는 `/admin/write-block`을 `{"blocked": false}`로 PUT해서 끈다. 켜져 있으면 precheck가 `already has an entry write block`으로 ABORT_PRE_WRITE가 된다.
- **Edge 기준 상태:** Edge 기록의 마지막 작업이 가중치 0이어야 한다(`origin_baseline_required`). 이전 실행이 100%로 끝났으면 Edge 컨트롤러로 0을 실행한다.
- **epoch:** Executor 펜스(`executor.resource_fences.max_epoch`)보다 큰 값이어야 한다. 아니면 접수되지 않고 SAFE_HOLD가 된다. 앞선 실행이 계획 버전 7을 쓴 경우 `[epoch]`로 8 이상을 준다.
- **Executor 권한:** 앱·pooler Pod가 바뀌면 Executor exec 권한(Role resourceNames)을 다시 줘야 한다.
- **쓰기 전 중단 뒤:** ABORT_PRE_WRITE가 난 계획은 CUTOVER에 남는다. 새 계획으로 다시 돌린다.
