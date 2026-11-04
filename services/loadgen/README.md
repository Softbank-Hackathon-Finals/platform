# 부하 생성기와 관측 (T18)

`demo-chat`에 쓰기 50 · 읽기 150 req/s를 넣고, 세 가지를 만든다.

- **대조 리포트:** 모든 시도를 DB 기록과 전수로 맞춰 실패 · 유실 · 중복을 센다(NFR-2).
- **창 요약:** 10초마다 `SliWindow`를 낸다. SHIFT · WATCH 판정의 유일한 입력이다([T36 계약](../../docs/observability/README.md)).
- **쓰기 공백 · 잠금 지연:** 쓰기 응답 사이 최대 간격과, 초당 50건일 때 쓰기 잠금 대기 시간을 잰다.

판정은 하지 않는다(T10 · T17). LLM을 쓰지 않는다. 저장소 checkout에서 실행한다. 창 내용은 T36 기준 구현 [`bravo_observability.build_window`](../observability/)가 만들고, 창 스키마는 `docs/observability/sli.schema.json`을 그대로 읽는다.

## 실행

```bash
uv sync
read -rs LOADGEN_DB_DSN && export LOADGEN_DB_DSN          # 대조용. messages SELECT 권한이면 된다. 읽기 전용 트랜잭션만 연다
read -rs LOADGEN_LOCK_DSN && export LOADGEN_LOCK_DSN      # 잠금 측정용. PgBouncer 뒤가 아니라 DB에 직접 붙는다
read -rs BRAVO_WINDOW_TOKEN && export BRAVO_WINDOW_TOKEN  # 창 전송 인증. --window-url을 쓸 때 필요하다
uv run bravo-loadgen run \
  --base-url https://<Edge 주소> --measurement-path edge \
  --plan-id plan-demo --source-env aws --target-env onprem \
  --duration 300 --out out/run1 \
  --db-dsn-env LOADGEN_DB_DSN --lock-dsn-env LOADGEN_LOCK_DSN \
  --window-url https://<플랫폼 수신 주소>
```

`read -rs`는 입력을 화면과 셸 기록에 남기지 않고 받는다. `bash`에서 **한 줄씩** 실행하고, 줄마다 값을 붙여 넣은 뒤 Enter를 누른다. 여러 줄을 한꺼번에 붙여 넣으면 다음 명령이 값으로 들어가고, 밀린 값이 명령으로 실행돼 화면과 셸 기록에 남는다. `sh`(dash)에는 `-s`가 없어 실패한다.

연결 문자열과 창 전송 토큰은 비밀이라 환경변수로만 받는다. 값은 파일 · 로그에 남기지 않는다. `--base-url` · `--window-url`에 자격 증명을 넣으면 거부한다. 이미 결과가 있는 `--out` 폴더도 거부한다. 새 실행이 죽었을 때 이전 판정이 남아 잘못 읽히지 않게 하기 위해서다.

| 종료 코드 | 뜻 |
|---|---|
| 0 | 실패 · 유실 · 중복 · 내용 불일치 · 유령 행이 모두 0 |
| 1 | 하나라도 있음 |
| 2 | 시험 불가 (DB를 못 읽음, 기록이 불완전, 쓰기 0건, 설정 오류, 예상 못 한 오류). 통과로 읽지 않는다 |

컷오버 뒤 지금 쓰기를 받는 DB와 다시 맞추려면 `bravo-loadgen reconcile --out out/run1 --db-dsn-env ...`. 창 파일만 검사하려면 `bravo-loadgen check-windows out/run1/windows.jsonl`.

산출물:

| 파일 | 내용 |
|---|---|
| `journal.jsonl` | 모든 시도. 논리 요청 ID, 시도 ID, 시각, 결과. 요청 ID는 여기와 리포트에만 둔다 |
| `windows.jsonl` | 낸 창 요약. 끝나면 `run-state.json`, `report.json`, `report.md`도 남는다 |

공개 증거로 올릴 때는 `report.*` · `windows.jsonl`만 올리고, 실제 환경 주소는 [공개 증빙 절차](../../docs/public-evidence-policy.md)로 가린다. 측정 경로(`--measurement-path`)는 사람이 선언한 값이다. `edge`는 실제 Edge 주소로 돌릴 때만 쓴다.

## 동작

**부하.** 예정 시각마다 보낸다(open-loop). 응답을 기다려 다음 요청을 보내면 서버가 멈춘 동안 요청 자체가 줄어 장애가 숫자에서 사라진다. 예정보다 늦게 보내면 부하 생성기 자신이 병목이므로 `GENERATOR_LAGGING`으로 경고한다.

**재시도.** 논리 요청 ID가 곧 멱등 키다. 시도마다 시도 ID(`<논리 요청 ID>.<순번>`)가 새로 붙고 본문은 그대로다. 규칙은 브라우저(`demo-chat` `transport.js`)와 같다. 5xx · 429 · 시간 초과 · 연결 실패 · 저장 확인이 안 되는 2xx는 다시 보내고, `Retry-After`를 지키고, 논리 요청당 60초 · 32회까지 한다. 다른 4xx는 다시 보내지 않는다. 시도 하나의 시간 초과는 15초다(설계 5.3). 읽기는 다시 보내지 않는다.

**대조.** 논리 쓰기 요청 하나는 아래 중 정확히 하나다. 행은 멱등 키와 본문의 요청 ID 양쪽으로 찾는다. 키만 보면 다른 키로 들어간 복사본을 놓친다.

| 상태 | 뜻 | NFR-2 |
|---|---|---|
| `confirmed` | 확인 응답을 받았고 같은 내용의 행이 하나 있다 | |
| `lost` | 확인 응답을 받았는데 행이 없다 | 유실 |
| `duplicated` | 행이 둘 이상이다 | 중복 |
| `failed_not_stored` | 끝내 확인 응답을 못 받았고 행도 없다 | 실패 |
| `failed_but_stored` | 끝내 확인 응답을 못 받았지만 행은 있다 | 실패 |
| `integrity_conflict` | 409를 받았다 | 실패 |
| `content_mismatch` | 행이 하나지만 내용이나 응답의 메시지 ID가 다르다 | FAIL |

이번 실행의 표시가 있는데 보낸 적 없는 행은 `phantom_rows`다. 역시 FAIL이다.

**쓰기 공백.** 부하를 넣는 동안 성공한 쓰기 응답 사이의 최대 간격이다. 503은 쓰기가 멈췄다는 신호라 간격을 끊지 않는다. 부하 시작부터 첫 성공까지, 마지막 성공부터 부하 끝까지도 간격으로 본다. 부하가 끝난 뒤 받은 재시도 응답은 세지 않는다. 공식 쓰기 정지 시간은 Executor 타이머(8.6)이고 이 값은 체감 교차 확인용이다.

**잠금 지연.** `demo-chat`은 커밋 순서를 id 순서와 맞추려고 모든 메시지 쓰기를 advisory lock 하나로 줄 세운다(`app/store.py`의 `MESSAGE_WRITE_LOCK`). 같은 잠금을 0.2초마다 자동 커밋 문장 하나로 잡아 대기 시간을 DB 시계로 재고, 그때 줄 선 세션 수도 읽는다. 문장이 끝나면 풀리므로 측정기 쪽 왕복만큼 앱을 막지 않는다. 부하 전 기준값과 부하 중 값을 나눠 보고한다. 표본 하나가 오래 막히면 그동안 다음 표본을 못 잡으므로, 일정 간격으로 왔을 요청들이 봤을 대기까지 넣어 백분위를 낸다. DB가 통째로 멈추면 DB 시계에는 안 보이므로 클라이언트가 기다린 시간으로 `LOCK_PROBE_STALLED`를 띄운다. 앱의 키가 바뀌면 `lock_probe.py`의 값도 바꾼다(종단 시험이 실제 값과 맞춰 본다).

**창 요약.** 응답마다 `Record`를 쌓아 창이 닫힐 때 `build_window`를 부른다. 코드를 두 벌로 두지 않으려고 경로 의존(`tool.uv.sources`)으로 묶었다. 이 때문에 CI 선택기(`scripts/ci_scope.py`)는 언어 프로젝트를 건드리는 PR마다 전체 검사를 고른다([CI 안내](../../docs/ci.md)). T18이 정하는 것은 창 경계와 집계 검사다. [계약 4절](../../docs/observability/README.md)에 더해 여기서 정한 것:

- 요청은 **응답을 받은 시각**의 창에 센다. 보낸 시각으로 세면 시간 초과(15초)가 끝날 때까지 창을 닫지 못해 10초 판정을 맞출 수 없다. 재시도도 시도마다 한 건이다.
- 끝나지 않은 마지막 창은 길이가 달라 내지 않는다(`WINDOW_MISMATCH`를 피한다). 대신 건수를 리포트에 남겨 `창 합계 + 꼬리 = 전체 시도`를 확인한다.
- 비어 있는 창도 낸다. 0건이 그대로 보여야 판정이 표본 부족으로 멈춘다. 표본을 부풀리거나 끊긴 창을 채우지 않는다.
- 전달에 실패한 창은 다시 보내지 않고 세기만 한다. 늦은 창은 판정에 쓰이지 않고, 없는 창은 UNKNOWN이다.
- `received_at`은 `build_window`가 창 끝으로 채운다. 플랫폼이 `receive()`로 덮어쓴다.

**지금 demo-chat에는 `X-Bravo-Env`가 없다.** 계약대로 모든 응답이 `unattributed.other`, 곧 오류로 세어진다. 리포트는 HTTP 오류율과 계약 오류율을 나란히 보이고 `ENV_HEADER_MISSING`을 띄운다. 헤더는 T22가 추가한다. 이 상태의 창을 판정에 넣으면 전부 FAIL이 된다.

리포트의 `sli_usable_for_judgement`는 Edge 경유이고, 환경을 모두 알 수 있고, 창이 온전할 때만 `true`다. 종료 코드는 쓰기 대조(NFR-2)만 본다. 읽기 5xx는 `HTTP_ERRORS` 경고와 창의 오류로 드러난다.

## 실행 위치와 창 전송

**실행 위치는 별도 소형 EC2다. Terraform 스택은 [`infra/envs/loadgen-aws`](../../infra/envs/loadgen-aws/README.md)에 있지만 EC2는 아직 만들지 않았다.** 측정과 데모 때만 켠다. 이유는 둘이다.

- 판정 입력은 평가 대상 밖에서 사용자와 같은 경로로, Edge를 거쳐 재야 한다(ADR-0010). 출발지 · 목적지 클러스터 안에 두지 않는다.
- 플랫폼 노드는 CPU 2,000m 중 1,976m가 이미 예약돼 있어(#146) 같은 서버에 두지 않는다.

필요한 사양의 근거는 [증거 폴더](../../docs/evidence/t18-load-generator-2026-11-04/README.md)의 CPU · 메모리 측정값이다. 대조와 잠금 측정은 DB에 닿아야 하므로, 이 EC2에서 DB로 가는 읽기 경로와 계정은 EC2와 별도로 정한다. 부하만 먼저 돌리고 `reconcile`은 DB에 닿는 곳에서 따로 돌려도 된다.

**창은 플랫폼 수신 경로로 POST한다.** `--window-url`은 창 JSON을 그대로 보내고 2초 안에 2xx를 못 받으면 전달 실패로 센다. Tailscale ACL은 부하 생성기 태그만 허용한다([계약 6절](../../docs/observability/README.md)). 수신 주소는 아직 없다(T10 · T17). 정해질 때까지는 `windows.jsonl`이 기준이다.

**창 전송은 `Authorization: Bearer <토큰>`으로 인증한다.** 계약 3절의 "창 수신 경로는 부하 생성기 주체만 쓸 수 있다"를 지키기 위해서다. 토큰은 받는 쪽(플랫폼 창 수신 경로)에서 정한다. 여기서 만들지 않는다.

- 토큰은 `BRAVO_WINDOW_TOKEN`에서만 읽는다. 명령줄 인자는 프로세스 목록에 보이기 때문이다.
- `--window-url`을 줬는데 토큰이 없거나 헤더에 넣을 수 없는 값(RFC 6750 `b64token` 밖)이면, 부하를 보내기 전에 종료 코드 2로 멈춘다.
- **`127.0.0.1`도 예외로 두지 않는다.** 코드 경로를 하나로 두어 로컬 시험이 실제 전송을 그대로 재게 하고, 포트 포워딩이면 로컬 주소의 끝이 실제 수신 경로일 수 있어서다. 로컬 시험은 아무 값이나 넣으면 된다.
- 토큰은 창 전송에만 붙는다. 부하 대상에는 보내지 않고, 수신 경로의 리다이렉트는 따라가지 않고 전달 실패로 센다.
- 토큰이 틀려 거부되면 다른 전달 실패와 같다. 부하는 끝까지 가고 `WINDOW_DELIVERY_FAILED`가 뜨며, 받는 쪽에는 창이 없으니 판정은 UNKNOWN으로 멈춘다.
- 평문 `http://`는 막지 않는다. Tailscale 안의 경로는 Tailscale이 암호화한다. 그 밖의 경로로 보낼 때는 `https://`를 쓴다.

## 검사

```sh
uv sync && uvx ruff check . && uv run pytest
TEST_DATABASE_URL=postgresql://... uv run pytest   # 실제 Postgres가 있을 때 DB 시험까지
uv run python testing/mutation_check.py            # 코드를 망가뜨려 시험이 잡는지 본다
```

CI에는 `TEST_DATABASE_URL`이 없어 DB 시험(`test_db.py`, `test_lock_probe.py`의 일부)은 건너뛴다. 이 부분은 로컬에서 돌린 결과를 증거 폴더에 남긴다.

## 종단 시험

```sh
git clone https://github.com/Softbank-Hackathon-Finals/demo-chat.git <폴더>
uv run python testing/e2e.py --demo-chat <폴더> --out <산출물 폴더>
```

실제 `demo-chat`(수정 없음)과 실제 Postgres를 띄워 일부러 유실 · 중복 · 실패를 넣고 잡히는지 본다. 약 17분 걸린다. Docker가 필요하고, 로컬에 이미 받은 이미지만 쓴다(`public.ecr.aws/docker/library/postgres:16`, `nginx:alpine`). 없으면 받지 않고 멈춘다. 실제 Cloudflare가 아니다. `testing/mock-edge.conf`는 가중치 분배와 헤더만 흉내 낸 대역이다. 결과는 [증거 폴더](../../docs/evidence/t18-load-generator-2026-11-04/README.md).

## 쓰기 잠금 실험

```sh
uv run python testing/lock_lab.py <calibrate|wire|ceiling|invariants|matrix> --variants <안별 demo-chat 폴더들의 상위 폴더> --out <산출물 폴더> --names <안...>
uv run python testing/lock_lab_report.py <결과.jsonl>...   # 결과를 표로 바꾼다
```

demo-chat 수정본 여러 벌을 같은 조건으로 잰다. 왕복 수를 중계기(`lock_lab_tap.py`)로 세고, 처리량 한계, 불변식(커밋 순서, 전파 건너뛰기, 멱등, 없는 첨부, PgBouncer transaction 모드), 왕복 지연 × 목적지 비중 행렬을 잰다. 모의 지연이고, 컨테이너는 `lock-lab-` 접두사로 띄운 뒤 지운다. 표의 판정은 창별 판정(`judge`)을 모은 것이고, 부하가 끝난 뒤 시작한 창은 결과에 그 근거(`judge_after_load`)가 있을 때만 뺀다. 결과와 재현 순서는 [증거 폴더](../../docs/evidence/t18-write-lock-lab-2026-11-04/README.md).
