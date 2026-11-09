# T18 합동 측정 절차

목적지 앱을 배포한 뒤, 김태현 님이 부하를 걸고 류지환 님이 Edge 가중치를 바꿔서 **가중치가 실제로 반영되는 시간과 환경별 p95를 같은 실행 ID로 남기는** 절차다.

**아직 실제 경로로 돌린 적이 없다.** 지금까지의 T18 값은 전부 로컬이고, 이 절차도 로컬 대역으로만 돌렸다([검증](#검증)). 이 문서를 따라 실제로 잰 값만 실측이라고 부른다.

```text
미리 정하기 → 목적지 쓰기 열기 → 사전 점검 → 1분 시험 실행 → 본 측정 + 가중치 변경 → 대조 → 영수증과 맞추기 → 보관
```

## 왜 하나

SHIFT 판정의 입력은 Edge를 지난 값이어야 한다([T36 계약](../observability/README.md)). 그런데 Edge 설정을 바꾼 시각과 요청이 실제로 나뉘는 시각은 다르고, 클라우드 사이 p95도 아직 모른다. 이 측정이 없으면 `unknown_limit`과 `p95_ms_max`가 제안값에 머문다. 이 문서는 값을 재는 절차다. 판정 기준을 바꾸는 일은 T36 담당이 결과를 보고 정한다.

## 1. 미리 정할 값

| 값 | 누가 | 어떻게 쓰나 |
|---|---|---|
| **실행 ID** | 김태현 → 류지환에게 전달 | `--run-id`와 가중치 영수증의 `run_id`에 **같은 값**을 쓴다. 소문자 · 숫자 · 하이픈, 32자 이내. 예: `t18-joint-1106-01` |
| 계획 ID | 류지환 | `--plan-id`. 창 수신에 연결한 계획과 같아야 한다 |
| 환경 값 | 류지환 | `--source-env` · `--target-env`. 두 앱의 `APP_ENV`다 |
| Edge 주소 | 류지환 | `--base-url`. 출발지 · 목적지 주소로 직접 보내지 않는다 |
| 창 수신 주소와 토큰 | 류지환 | `--window-url`과 `BRAVO_WINDOW_TOKEN`. EC2에서만 쓴다. 토큰 수명은 최대 8시간이니 측정 전에 받는다. 전달 경로는 [T10 창 수신부](../../services/platform-events/deploy/window/README.md) 4단계로 합의한다 |
| DB 읽기 계정 | 정할 일 | `LOADGEN_DB_DSN`(대조), `LOADGEN_LOCK_DSN`(잠금 측정). 닿는 곳이 없으면 대조를 나중에 한다 |

비밀 값은 환경변수로만 넣는다. 파일, 명령줄, 채팅에 적지 않는다. `bash`에서 **한 줄씩** 실행하고, 줄마다 값을 붙여 넣은 뒤 Enter를 누른다.

```bash
read -rs LOADGEN_DB_DSN && export LOADGEN_DB_DSN
```

```bash
read -rs LOADGEN_LOCK_DSN && export LOADGEN_LOCK_DSN
```

```bash
read -rs BRAVO_WINDOW_TOKEN && export BRAVO_WINDOW_TOKEN
```

## 2. 어디서 돌릴까

**본 측정은 EC2에서 한다. 노트북은 경로 확인과 예비 값에만 쓴다.** 노트북 값에는 집 · 사무실 회선과 노트북 상태가 함께 들어가기 때문이다.

| | 노트북 (`--generator laptop`) | EC2 (`--generator loadgen-ec2`) |
|---|---|---|
| 준비 | 저장소 checkout만 있으면 된다 | [`loadgen-aws`](../../infra/envs/loadgen-aws/README.md)를 적용하고 켠다. **아직 만들지 않았다** |
| 리포트 표시 | `GENERATOR_NOT_EC2` 경고, "판정 근거로 쓰지 않는다" 문구 | 표시 파일(`/var/lib/bravo-loadgen-ready`)이 있어야 이 값을 쓸 수 있다 |
| `sli_usable_for_judgement` | 항상 `false` | 다른 경고가 없으면 `true` |
| 창 보내기 | Edge 경로에서는 거부한다. 창에는 실행 위치 칸이 없어 받는 쪽이 구분하지 못한다 | `tag:bravo-loadgen`으로 보낸다 |
| 생성기 지연 | 잠들거나 바쁘면 `GENERATOR_LAGGING` | 같은 경고로 본다. 뜨면 사양을 올린다 |
| 회선 | 리포트의 부하 전 왕복 값으로 가늠만 한다 | 같은 값을 남긴다 |

**노트북에서 돌릴 때.** 전원을 꽂고, 되도록 유선으로, 다른 무거운 작업을 멈춘다. 명령 앞에 `caffeinate -i`를 붙이면 잠들지 않는다. 대상까지 닿는 DB 경로가 없으면 DB 옵션을 뺀다.

**EC2에서 돌릴 때.** [측정 때만 켜기](../../infra/envs/loadgen-aws/README.md#측정-때만-켜기)를 따른다. `tmux` 안에서 실행한다. Terraform 적용과 Tailscale 키 발급은 이 절차 밖의 일이고, 첫 실행 전에 [첫 실행 확인](../../infra/envs/loadgen-aws/README.md#첫-실행-확인)을 본다.

아래 명령은 모두 `services/loadgen`에서 실행한다. 처음 한 번 `uv sync`를 돌린다.

## 3. 사전 점검

부하 없이 점검만 한다. 다음 절의 `measure`도 같은 점검을 먼저 하므로, 여기서는 값이 맞는지 미리 보는 단계다.

**먼저 목적지 앱의 쓰기를 연다.** 목적지 앱은 쓰기 차단이 켜진 채 뜨고, 다시 뜨면 또 켜진다([쓰기 스위치](../../infra/envs/target-workloads/README.md#쓰기-스위치)). 켜진 채 재면 목적지로 간 쓰기가 전부 503이 되어 측정을 버린다. 저장소 루트에서 목적지 클러스터의 `KUBECONFIG`로 상태를 읽고, 닫혀 있으면 연다. `status`는 읽기만 한다.

```bash
infra/envs/target-workloads/write_switch.py status
```

```bash
infra/envs/target-workloads/write_switch.py open
```

노트북:

```bash
uv run bravo-loadgen preflight --base-url https://<Edge 주소> --measurement-path edge --generator laptop --plan-id <계획 ID> --source-env <출발지 값> --target-env <목적지 값> --run-id <실행 ID> --duration 600 --out out/<실행 ID>
```

EC2:

```bash
uv run bravo-loadgen preflight --base-url https://<Edge 주소> --measurement-path edge --generator loadgen-ec2 --plan-id <계획 ID> --source-env <출발지 값> --target-env <목적지 값> --run-id <실행 ID> --duration 600 --out out/<실행 ID> --window-url http://<수신 주소>:30981/v1/observability/windows --db-dsn-env LOADGEN_DB_DSN --lock-dsn-env LOADGEN_LOCK_DSN
```

종료 코드 0이면 통과, 2면 실패다. 실패해도 한 번에 고칠 수 있게 끝까지 보고 모두 보여 준다.

| 항목 | 무엇을 보나 | 실패하면 |
|---|---|---|
| 대상 상태 | `/api/health/db`가 5번 모두 200이고 본문이 `{"ok": true}`다. `/readyz`는 DB를 보지 않아 쓰지 않는다 | 앱이나 DB, Edge 경로를 확인한다 |
| 환경 헤더 | `X-Bravo-Env`가 계획에 등록한 값이다. 본 값과 횟수를 보여 준다 | 값이 `aws, aws`처럼 겹치면 Edge가 헤더를 덧붙인 것이다. `APP_ENV`와 Edge 설정을 본다 |
| Edge 경유 | `--measurement-path edge`일 때 응답마다 `CF-RAY`가 있다 | Edge를 지나지 않는 주소다 |
| 쓰기 차단 | 쓰기 부하가 있으면 `/api/info`를 5번 읽어 `X-Bravo-Env`별로 `write_blocked`를 센다. `true`가 한 번이라도 있으면 실패다. 필드가 없거나 읽지 못한 응답, 한 번도 나오지 않은 환경은 "확인 못 함"으로 적고 통과시킨다 | 목적지면 `write_switch.py open`으로 연다. 출발지면 출발지 담당에게 풀어 달라고 한다 |
| 창 수신 | 토큰을 붙여 빈 문서 `{}`를 보낸다. 수신부가 토큰 → 계획 연결 → 문서 순서로 보므로, `422 invalid_window`면 앞의 둘이 맞고 저장은 되지 않는다 | 401은 토큰, 403은 계획 연결 · 만료(계획이 바뀌어도 새 연결이 필요하다), 시간 초과는 Tailscale 경로를 본다 |
| 시계 | 창 수신의 `Date`(없으면 대상)와 3초 넘게 다르지 않다(`max_clock_skew_seconds` 2초 + `Date` 해상도 1초) | 시계를 맞춘다. 어긋나면 창이 `CLOCK_SKEW`로 전부 UNKNOWN이 된다 |
| 대조 · 잠금 DB | 옵션을 줬을 때만 접속해 읽어 본다 | 연결 문자열과 경로를 본다. 오류 내용은 종류만 나온다 |

**시작 전 가중치가 0이면 환경 헤더에 출발지 값만 보여야 한다.** 점검은 이것을 실패로 치지 않는다. 눈으로 확인한다. 같은 이유로 쓰기 차단 항목에는 목적지가 "응답 없어 확인 못 함"으로 나온다. 목적지는 위 `write_switch.py status`로 본다.

**점검이 못 보는 것.**

- 창 수신에서 `--plan-id`가 연결된 계획과 같은지는 진짜 창을 보내야 안다. 다음 시험 실행에서 확인한다.
- 쓰기 차단은 Pod마다 따로라 5번 안에 닿지 않은 Pod는 못 본다. Pod 전부는 `write_switch.py status`가 본다. 점검 뒤 목적지 Pod가 다시 뜨면 그 Pod는 다시 닫힌다.

## 4. 1분 시험 실행

본 측정 전에 경로만 확인한다. 새 코드 없이 기존 옵션으로 부하만 낮춘다. 실행 ID와 폴더는 본 측정과 다르게 쓴다.

```bash
uv run bravo-loadgen measure --base-url https://<Edge 주소> --measurement-path edge --generator <laptop 또는 loadgen-ec2> --plan-id <계획 ID> --source-env <출발지 값> --target-env <목적지 값> --run-id <실행 ID>-try --duration 60 --write-rps 5 --read-rps 15 --out out/<실행 ID>-try
```

EC2라면 3절의 `--window-url`과 DB 옵션을 같이 붙인다. 쓰기 300건이 실제 DB에 들어간다.

`report.md`에서 이것을 본다.

| 볼 것 | 기대 | 어긋나면 |
|---|---|---|
| 경고 `WINDOW_DELIVERY_FAILED` | 없음 | `--plan-id`가 연결된 계획과 다르거나 토큰이 만료됐다 |
| 경고 `ENV_HEADER_MISSING` · `ENV_HEADER_UNREGISTERED` | 없음 | 일부 응답이 앱이 아닌 곳에서 왔다 |
| 경고 `GENERATOR_LAGGING` | 없음 | 부하 생성기가 병목이다. 낮은 부하에서 뜨면 본 측정은 더 나쁘다 |
| 실패 / 유실 / 중복 | 0 / 0 / 0. DB 옵션이 없으면 `NOT_RECONCILED`, 종료 코드 2 | 먼저 원인을 찾는다 |

**창이 표본 부족으로 UNKNOWN이고 `align`이 `NOT_OBSERVED`인 것은 정상이다.** 창마다 요청이 약 200건이라 판정 기준(1,000건)에 못 미친다. 시험 실행 값으로 반영 시간이나 p95를 말하지 않는다.

### 본 측정 직전 예열

시험 실행 뒤 본 측정을 시작하기 바로 전에, 읽기만 초당 50건으로 30초 보낸다. 쓰기와 창 전송은 하지 않고 결과도 판정에 쓰지 않는다.

```bash
uv run bravo-loadgen measure --base-url https://<Edge 주소> --measurement-path edge --generator <laptop 또는 loadgen-ec2> --plan-id <계획 ID> --source-env <출발지 값> --target-env <목적지 값> --run-id <실행 ID>-warm --duration 30 --write-rps 0 --read-rps 50 --out out/<실행 ID>-warm
```

DB 옵션이 없어 `NOT_RECONCILED`, 종료 코드 2로 끝나는 것이 정상이다. HTTP 오류와 시간 초과가 0인지만 본다. 있으면 본 측정을 시작하지 않고 한 번 더 예열한다.

왜 넣었나. 2026-11-06 04:17 KST에 같은 Edge 경로로 예열 없이 처음 보낸 읽기 200건/초 · 60초 실행은 오류율이 66%였다(502 366건 · 시간 초과 7,550건, [T17 기록](../evidence/t17-edge-connectivity-2026-11-06/README.md)). 그 뒤 50건/초 → 200건/초 순서로 다시 재니 용량을 바꾸지 않고 오류가 0이었다. 같은 시각 출발지 앱과 터널은 CPU 한도에 거의 걸리지 않았고 앱 재시작도 없었다. 502 응답에는 앱 헤더가 없었다. 그래서 Edge 쪽 경로가 처음에 느렸던 것으로 보지만 **원인은 확정하지 못했다.** 예열은 이 차이를 줄이려는 운영 단계이고, 고쳤다는 근거는 아니다.

## 5. 본 측정과 가중치 변경

**김태현.** 예열이 깨끗하면 실행 ID를 전달한 뒤 바로 시작한다. `--duration 600`은 아래 일정에 여유를 더한 값이다. 쓰기 30,000건이 실제 DB에 남는다. 이 절차에는 지우는 단계가 없다.

```bash
uv run bravo-loadgen measure --base-url https://<Edge 주소> --measurement-path edge --generator loadgen-ec2 --plan-id <계획 ID> --source-env <출발지 값> --target-env <목적지 값> --run-id <실행 ID> --duration 600 --out out/<실행 ID> --window-url http://<수신 주소>:30981/v1/observability/windows --db-dsn-env LOADGEN_DB_DSN --lock-dsn-env LOADGEN_LOCK_DSN
```

노트북이면 `--generator laptop`으로 바꾸고 `--window-url`과 닿지 않는 DB 옵션을 뺀 뒤 앞에 `caffeinate -i`를 붙인다.

**류지환.** 첫 줄에 나온 "사전 점검 통과: 실행 ID ..."를 받은 뒤 가중치를 바꾼다. 한 단계는 창(10초) 10개 이상이 되도록 2분씩 둔다. 반영 시간을 아직 모르기 때문이다.

| 시작 뒤 | 가중치 | 왜 |
|---|---|---|
| 0~1분 | 0% | 기준값. 출발지만 나와야 한다 |
| 1분 | 10% | |
| 3분 | 50% | |
| 5분 | 100% | |
| 7분 | 0% | 되돌림 |
| 9분 | | 그대로 두고 부하가 끝나기를 기다린다 |

변경마다 영수증을 남긴다. T17 `Controller.execute`가 돌려주는 값을 JSON 한 줄씩 저장한 파일이 그대로 입력이 된다. `align`이 쓰는 값은 아래와 같고 나머지는 무시한다.

| 값 | 뜻 |
|---|---|
| `run_id` · `plan_id` | **본 측정과 같아야 한다.** 다르면 `align`이 거부한다 |
| `weight_target` | 0 · 10 · 50 · 100 |
| `request_at` | 변경을 요청한 시각. 시간대가 있어야 한다 |
| `status` | `CONFIRMED`만 단계로 센다. `UNCERTAIN`이면 T17 절차대로 중단하고 인계한다 |
| `confirmed_at` · `reason` · `revision` | 있으면 함께 적는다 |

`lb_ref` · `operation_id` 같은 운영 식별자는 결과로 옮기지 않지만, 영수증 파일 자체는 저장소에 넣지 않는다.

**판정 실패로 중간에 0%로 돌아가면** 완료된 4단계 측정으로 부르지 않는다. 자동 복귀 시험으로 따로 남긴다([T17](../../services/edge-control/README.md)).

## 6. 끝난 뒤: 대조 → 창 검사 → 맞추기

순서를 지킨다. 대조가 리포트를 고치고, `align`은 그 리포트를 읽는다.

**① 대조.** `measure`에 대조 DB를 줬으면 이미 끝났다. 안 줬으면 종료 코드 2(`NOT_RECONCILED`)가 정상이고, DB에 닿는 곳에서 돌린다. 그 전에 `journal.jsonl` · `run-state.json` · `windows.jsonl`을 옮겨 둔다.

```bash
uv run bravo-loadgen reconcile --out out/<실행 ID> --db-dsn-env LOADGEN_DB_DSN
```

종료 코드 0은 실패 · 유실 · 중복 · 내용 불일치 · 유령 행이 모두 0, 1은 하나라도 있음, 2는 시험 불가다. 2를 통과로 읽지 않는다.

**② 창 검사.**

```bash
uv run bravo-loadgen check-windows out/<실행 ID>/windows.jsonl
```

**③ 영수증과 맞추기.** 영수증 파일은 저장소 밖에 둔다.

```bash
uv run bravo-loadgen align --out out/<실행 ID> --receipts <영수증 파일>
```

`out/<실행 ID>/weight-alignment.json`이 생긴다. 종료 코드 0은 모든 단계가 창에서 확인됨, 1은 확인 못 한 단계가 있음, 2는 다른 실행의 영수증이거나 입력이 잘못됨이다.

## 7. 결과 읽는 법

| 값 | 뜻 | 조심할 점 |
|---|---|---|
| `reflect_upper_bound_s` | `request_at`부터, 그 뒤 **통째로 새 가중치인 첫 창의 끝**까지 | 상한이다. 창이 10초라 실제 반영은 이보다 빠를 수 있다. 시계 차이도 들어간다 |
| `provider_confirmed_after_s` | provider가 설정을 돌려준 시각 | 실제 분배 반영이 **아니다**. 반영 시간으로 쓰지 않는다 |
| `observation` | `OBSERVED` · `NOT_OBSERVED` · `RECEIPT_NOT_CONFIRMED` | 앞뒤 변경이 섞인 창, 표본이 적은 창, 비율이 안 맞는 창은 증거로 쓰지 않는다 |
| `source_p95_ms_max` · `target_p95_ms_max` | 그 단계 창들의 환경별 p95 중 최댓값 | 창별 값은 `windows`에 있다. p95는 합칠 수 없어 평균 내지 않는다 |
| `reference_verdict` | T36 `judge`로 다시 읽은 참고값 | 기준은 T10이 저장한 판정이다 |

**실측이라고 말할 수 있는 경우는 하나다.** `measurement.path`가 `edge`, `generator`가 `loadgen-ec2`이고 `sli_usable_for_judgement`가 `true`일 때다.

| 리포트 | 이렇게 적는다 |
|---|---|
| `edge` + `loadgen-ec2` + 사용 가능 `true` | Edge 경유 실측 |
| `edge` + `laptop` | 노트북에서 잰 Edge 경유 값. 회선이 포함됐고 판정 근거가 아니다 |
| `edge`인데 `PREFLIGHT_SKIPPED` 또는 다른 경고 | 경고 이름과 함께 적는다. 실측이라고 쓰지 않는다 |
| `local-*` | 로컬 대역 값 |

## 8. 보관

| 파일 | 어디에 |
|---|---|
| `report.json` · `report.md` · `windows.jsonl` · `weight-alignment.json` · `preflight.json` | [공개 증빙 절차](../public-evidence-policy.md)로 실제 주소를 가린 뒤 `docs/evidence/`에 |
| `journal.jsonl` · `run-state.json` · 영수증 | 저장소 밖 비공개. `reconcile`을 다시 돌리려면 필요하다 |

EC2를 쓰면 [결과 가져오기](../../infra/envs/loadgen-aws/README.md#측정-때만-켜기)로 받은 뒤 인스턴스를 끈다. 파일 목록에 `preflight.json`을 더한다.

## 검증

전부 2026-11-06 로컬에서 돌렸다. **실제 Edge, 클러스터, Cloudflare, EC2에는 요청을 한 건도 보내지 않았다.** 원자료는 [증거 폴더](../evidence/t18-joint-rehearsal-2026-11-06/README.md)에 있다.

| 검사 | 결과 |
|---|---|
| `services/loadgen`의 `uv run pytest` | 237 통과, 10 건너뜀 (CI와 같은 조건) |
| `TEST_DATABASE_URL`을 준 `uv run pytest` | 247 통과 |
| `uvx ruff==0.17.3 check .` | 통과 |
| 변이 시험 | 99건 중 99건 잡음. 사전 점검 · 노트북 표시 · 맞추기 변이 32건을 더했다. 처음 돌렸을 때 2건을 놓쳐 시험을 더한 뒤 다시 돌린 값이다 |
| 전체 순서 리허설 | 실제 demo-chat과 Postgres에 Edge · 창 수신 대역을 붙여 두 번. 검사 17건씩, 실패 0건. 두 번 모두 다른 세션의 Docker 시험과 겹쳐 `GENERATOR_LAGGING`이 떴다 |
| 실제 T10 수신부 코드 | 맞는 토큰 · 틀린 토큰 · 짧은 토큰 · 꺼진 연결 · 진짜 창 · 다른 계획을 돌림. 사전 점검은 창을 저장하지 않았다. 계획이 다른 경우만 시험 실행에서 드러났다 |

**쓰기 차단 항목을 더한 뒤 (2026-11-06, 로컬).** 위 리허설은 이 항목이 생기기 전 코드로 돌렸다.

| 검사 | 결과 |
|---|---|
| `uv run pytest` | 248 통과, 10 건너뜀 (CI와 같은 조건). `TEST_DATABASE_URL`을 주면 258 통과 |
| 변이 시험 | 108건 중 108건 잡음. 쓰기 차단 변이 9건을 더했다. 처음에는 1건을 놓쳤는데, 변이 문장이 들여쓰기를 깨 시험이 수집 오류로 끝난 탓이었다. 변이를 고치고 본문이 열림인 503 시험을 더한 뒤 다시 돌린 값이다 |
| `preflight-write-block` 시나리오 | 실제 demo-chat `57d7ce4`의 `/api/info`로 검사 5건, 실패 0건. 대역 가중치 100%에서 목적지를 닫으면 실패하고 `write_switch.py open`을 알렸다. 0%에서는 목적지가 "응답 없어 확인 못 함"이었다. 출발지를 닫으면 실패했다. 행은 남지 않았다 |

**재지 못한 것**

- 실제 Cloudflare 응답의 `CF-RAY`, 실제 가중치 반영 시간, 클라우드 사이 p95. 로컬 대역의 반영 시간은 nginx 설정을 다시 읽는 시간이라 실제 값의 근거가 아니다
- 실제 tailnet의 창 수신 왕복과 2초 전송 제한, 실제 Secret으로 발급한 토큰과 계획 연결
- `loadgen-aws` EC2에서의 실행. 표시 파일 검사는 시험 안에서 임시 파일로 확인했다
- 노트북 회선이 p95에 주는 영향의 크기. 왕복 기준값을 남길 뿐 빼서 보정하지 않는다
- T17 `Controller`가 만든 실제 영수증. 시험은 같은 필드로 직접 만든 영수증을 썼다
