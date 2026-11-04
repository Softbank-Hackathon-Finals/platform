# [T18] 쓰기 잠금 실험 — 추천: 안 3+읽기(쓰기는 파이프라인 한 묶음, 읽기는 자동 커밋). 모든 쓰기 경로가 1왕복이 되고 잠금 · INSERT 문장과 `statement_timeout`을 그대로 둔다. 목적지 50%에서 원본은 실제 왕복 약 11ms까지 버티고, 이 안은 60ms까지 5회 모두 PASS, 80ms는 3회 중 2회 PASS(1회는 멈춤으로 제외, 원인 미확인)다

**결론.**

- **가설은 맞다.** 원본은 잠금을 쥔 채 3왕복을 쓴다. 처리량은 `1 / (3 × 왕복 + s)` 예측의 0.94~1.00배로 설명된다.
- **잠금만의 문제는 아니다.** 밀린 쓰기가 앱의 DB 연결 10개를 모두 잡아 `PoolTimeout`이 나고, 읽기와 출발지 사용자까지 실패한다. 잠금을 고쳐도 읽기가 3왕복이면 왕복 42ms · 목적지 50%가 여전히 FAIL이다. 그래서 읽기까지 고친 안을 추천한다.
- **추천안의 경계.** 안 3+읽기는 목적지 50%에서 설정 60ms(실제 약 62ms)까지 5회 모두 PASS, 설정 80ms(실제 약 82ms)는 3회 중 2회 PASS(1회는 멈춤으로 제외, 원인 미확인), 설정 100ms는 2회 모두 FAIL이다.
- **유실 · 중복은 어느 칸에서도 없었다.** 데모를 막는 것은 데이터 손상이 아니라 judge 판정이다. FAIL이면 가중치가 0으로 돌아간다.
- **모의 지연이다. 실제 클라우드 사이 값이 아니다.** 실제 왕복을 아직 모른다. 측정 기준으로 실제 왕복이 약 11ms 이하면 원본도 PASS, 약 13ms부터 FAIL이다. 패치가 필요한지는 실제 값(T8)으로 정해야 한다.
- demo-chat에는 push하지 않았다. `patches/`는 담당자(T22)와 논의할 초안이다.

| 안 | 목적지 | 마지막 PASS(실제 왕복) | 처음 FAIL(실제 왕복) | 회차별 결과 |
|---|---:|---:|---:|---|
| 원본 | 50% | 10ms(11.1ms) | 12ms(13.4ms) | 0ms PP · 5ms PP · 8ms P · 10ms PP · 12ms FF · 15ms F · 20ms FF · 40ms FF |
| 안 1 | 50% | 15ms(16.3ms) | 20ms(21.4ms) | 0ms PP · 5ms PP · 8ms P · 10ms P · 12ms P · 15ms PP · 20ms FF · 40ms FF |
| 안 2 | 50% | 20ms(21.2ms) | 40ms(42.2ms) | 0ms PP · 5ms PP · 20ms PP · 40ms FF |
| 안 3 | 50% | 35ms(37.2ms) | 40ms(41.9ms) | 0ms PP · 5ms PP · 20ms PP · 25ms P · 30ms P · 35ms PP · 40ms FF |
| 안 2+읽기 | 50% | 80ms(81.8ms) | 100ms(102.5ms) | 0ms PP · 5ms PP · 20ms PP · 40ms PP · 60ms ESSPPP · 80ms SSP · 100ms SFF · 120ms F |
| 안 3+읽기 | 50% | 80ms(81.6ms) | 100ms(102.3ms) | 0ms PP · 5ms PP · 20ms PP · 40ms PP · 60ms PPPPP · 80ms SPP · 100ms FF · 120ms F · 150ms F |

마지막 PASS와 처음 FAIL의 왕복은 부하 중에 잰 **실제 왕복**이다. 회차별 결과의 P · F는 judge 판정이고, S는 원인 미확인 멈춤, E는 Edge 대역이 만든 502다(10절). S · E 회차는 경계 계산에서 뺐다. **그래서 안 3+읽기의 80ms는 3회 중 1회를 뺀 값이다.**

## 1. 측정 환경

| 항목 | 값 |
|---|---|
| 측정 코드 | 측정은 #158 브랜치의 `21085aa` 위에서 했다. 그 뒤 `dea9ced`(창 전송 Bearer 인증)로 rebase했고, #158이 머지된 뒤 develop `399f297`을 머지 커밋으로 합쳤다. `services/loadgen/testing`의 Git 트리 해시 `7b4c6c98a4a2dd78de9541f61b6b7d849bd8175a`는 rebase 뒤 값이고 머지로 바뀌지 않았다. develop이 `services/loadgen`을 바꾸지 않아 머지 뒤에는 다시 재지 않았다. 측정 뒤에 고친 도구는 결과를 읽는 코드(`lock_lab_report.py`, Edge 오류 집계)뿐이다. rebase 뒤에 안 3+읽기 설정 20ms · 50% 1칸을 다시 돌려 도구가 도는지 확인했다(P, 5xx 0, 쓰기 p95 27ms, 실패 · 유실 · 중복 0 · 0 · 0). `matrix-smoke-after-rebase.jsonl` |
| 대상 앱 | demo-chat main `dc9bece24898b752a77af1d1bbd440c11100e5f2`. `app/store.py`는 #158에서 쓴 `b71d4aa`와 같다. 안마다 `app/store.py`만 바꾼 복사본을 저장소 밖에서 썼다. 환경마다 uvicorn 프로세스 1개로 직접 실행(컨테이너 이미지 아님) |
| DB | 행렬: `public.ecr.aws/docker/library/postgres@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a`(16). 불변식은 16과 18.4 `@sha256:a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636` 둘 다. 실제 DB는 18.4다 |
| PgBouncer | `ghcr.io/cloudnative-pg/pgbouncer:1.25.1@sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8`. 설정은 `infra/envs/source-workloads/pgbouncer.ini`와 같은 값(transaction 모드, `default_pool_size=10`, `max_prepared_statements=0`). **측정 때는 목적지 설정이 저장소에 없어 같다고 가정했다.** develop을 합치며 들어온 `infra/envs/target-workloads/pgbouncer.ini`의 세 값도 같다 |
| Edge 대역 | `nginx@sha256:df221db836e1754089190208cee7eeda94f233197056426eda74a43ab1abeac2`. 가중치 분배와 `X-Bravo-Env`만 흉내 낸다. 실제 Cloudflare가 아니다 |
| 지연 | `testing/tcp_fault.py`가 패킷을 받아 정해진 시간만큼 늦춘다. 지터 · 손실 없음. **모의 지연이다** |
| 부하 | 쓰기 50 · 읽기 150 req/s, 칸마다 30초. judge는 `window_seconds=10`, `min_requests=1000`, `error_rate_max=0.01`, `p95_ms_max=1500`(자리값) |
| 측정 머신 | Apple M5, 논리 코어 10개, 메모리 32 GiB, Python 3.14.7 |
| 측정 시각 | 부하 칸은 2026-11-04 04:24~07:22 KST, 보정 · 왕복 수 · 불변식은 03:3x~04:24다. 전달받은 인터넷 끊김 시간대(02:3x~03:10, 07:30~09:20 무렵)와 겹치는 칸은 없다. 시각을 코드로 대조했다. `measurement-times.json` |
| 함께 돈 작업 | 다른 세션의 시험과 다른 프로젝트의 컨테이너(재시작 반복)가 함께 돌았다. 칸 시작 때 1분 부하 평균은 1.31~6.34. 칸마다 결과 JSON의 `load_average_at_start`에 있다 |

**설정 왕복과 실제 왕복은 다르다.** 중계기를 거치는 비용이 더해진다. 부하 없이 잰 값은 설정 0 · 5 · 10 · 20 · 40ms에 실제 1.74 · 8.04 · 14.38 · 25.40 · 45.68ms이고, 부하 중에는 칸마다 재서 표에 적었다(설정 20ms가 부하 중 약 21~23ms). DB에 직접 붙으면 1.59ms다. **#158의 "왕복 20ms"도 설정값이다.** `calibration.json`.

## 2. 가설 검증: 잠금 구간의 왕복과 처리량 한계

**왕복 수는 PG 프로토콜 메시지를 중계기에서 세었다**(`전체 왕복 / 잠금을 쥔 동안의 왕복`, 설정 20ms). 원본의 새 쓰기는 잠금 응답을 받은 뒤 INSERT · SELECT를 거쳐 COMMIT이 도착할 때까지 3왕복을 쥔다. `wire.json`.

| 안 | 새 쓰기 | 재전송 | 충돌 | 없는 첨부 | 새 쓰기의 잠금 점유 |
|---|---:|---:|---:|---:|---:|
| 원본 | 6 / 3 | 7 / 4 | 6 / 3 | 5 / 2 | 75.96ms |
| 안 1 | 8 / 2 | 9 / 3 | 6 / 3 | 5 / 2 | 49.31ms |
| 안 2 | 1 / 0 | 2 / 0 | 2 / 0 | 1 / 0 | 0.00ms |
| 안 3 | 1 / 0 | 1 / 0 | 1 / 0 | 1 / 0 | 0.22ms |
| 안 3(BEGIN) | 3 / 0 | 3 / 0 | 3 / 0 | 4 / 0 | 0.29ms |
| 안 2+읽기 | 1 / 0 | 2 / 0 | 2 / 0 | 1 / 0 | 0.00ms |
| 안 3+읽기 | 1 / 0 | 1 / 0 | 1 / 0 | 1 / 0 | 0.10ms |

안 2 · 안 3의 잠금 점유는 DB 안에서만 일어나 왕복이 들지 않는다. 안 2의 0.00ms는 한 메시지라 중계기로 잴 수 없다는 뜻이다.

**처리량 한계는 예측으로 설명된다**(앱 없이 Store에 동시 10개로 쉬지 않고 쓴 초당 건수, 8초, 타임아웃 5초). 칸마다 `실측 / 예측 (실측 ÷ 예측)`이다. 왕복은 부하 없이 잰 값, `s`는 설정 0ms 칸에서 구했다. `ceiling.json`.

| 안 | 한계 | 설정 5ms | 설정 10ms | 설정 20ms | 설정 40ms |
|---|---|---:|---:|---:|---:|
| 원본 | 잠금: 1 / (3왕복 + s) | 46.8 / 49.6 (0.94) | 25.5 / 25.5 (1.00) | 13.4 / 13.8 (0.97) | 7.4 / 7.5 (0.98) |
| 안 1 | 잠금: 1 / (2왕복 + s) | 72.2 / 73.8 (0.98) | 38.6 / 38.1 (1.01) | 20.4 / 20.7 (0.98) | 11.1 / 11.3 (0.99) |
| 안 2 | 풀: 10 / (1왕복 + s) | 1,562.0 / 1,253.5 (1.25) | 798.5 / 698.5 (1.14) | 418.8 / 394.7 (1.06) | 215.0 / 219.2 (0.98) |
| 안 3 | 풀: 10 / (1왕복 + s) | 1,615.5 / 1,083.1 (1.49) | 796.0 / 642.2 (1.24) | 372.9 / 376.1 (0.99) | 204.0 / 213.4 (0.96) |
| 안 3(BEGIN) | 풀: 10 / (3왕복 + s) | 468.9 / 442.7 (1.06) | 228.0 / 240.3 (0.95) | 128.4 / 133.9 (0.96) | 70.4 / 73.8 (0.95) |
| 안 3+읽기 | 풀: 10 / (1왕복 + s) | 1,608.5 / 1,076.9 (1.49) | 802.2 / 640.0 (1.25) | 385.8 / 375.3 (1.03) | 206.2 / 213.1 (0.97) |

원본과 안 1은 0.94~1.01배로 맞는다. 잠금이 한계라는 뜻이다. 잠금 구간에 왕복이 없는 안은 한계가 `풀 10 ÷ 왕복`으로 옮겨 가고, 처리량이 원본의 약 28배(설정 40ms)가 된다. 이 안들의 설정 5 · 10ms 칸이 예측보다 높은 것은 부하 없이 잰 왕복이 부하 중 왕복보다 길게 나왔기 때문으로 본다(설정 5ms: 8.04ms 대 6.0~6.9ms).

## 3. 왜 쓰기 5xx와 p95 5초가 되나

원본, 설정 20ms, 목적지 50%, 2회(#158의 쓰기 5xx 1,526건과 같은 규모로 재현됐다).

| 항목 | 1회차 | 2회차 |
|---|---:|---:|
| 쓰기 5xx / 읽기 5xx | 1,326 / 490 | 1,064 / 432 |
| 출발지 5xx(`QueryCanceled`) | 492 | 398 |
| 목적지 앱 오류 `QueryCanceled` / `PoolTimeout` | 436 / 853 | 372 / 718 |
| 재시도한 논리 쓰기 | 652 | 586 |
| 실패 / 유실 / 중복 | 0 / 0 / 0 | 0 / 0 / 0 |

1. 잠금 점유가 3왕복, 약 76ms다. 목적지로 가는 쓰기가 초당 25건이면 줄이 줄지 않는다(25 × 3왕복이 1초를 넘는다).
2. 줄을 선 쓰기가 잠금 대기 제한(`statement_timeout` 200ms)을 넘어 `QueryCanceled`로 5xx가 된다. **지연이 없는 출발지 사용자도 같은 줄에 서므로 함께 실패한다.**
3. 목적지 앱은 밀린 쓰기와 읽기가 연결 10개를 다 쓴다. 나머지는 5초를 기다리다 `PoolTimeout`으로 503이 된다. **p95 5.3초의 정체가 이것이고, 읽기도 여기서 실패한다.**
4. 클라이언트 재시도가 이 칸의 쓰기를 모두 살렸다. 유실 · 중복은 없다. 원본 설정 40ms · 목적지 50%에서는 끝내 확인하지 못한 쓰기(실패)가 직접 경로 9 · 9건, PgBouncer 경로 16 · 50건 나왔다.

목적지 10%도 안전하지 않다. 설정 40ms에서 오류율만으로 FAIL이고 5xx는 대부분 출발지다(89 대 10, 160 대 16). 잠금 점유가 길어져 출발지 쓰기가 200ms 제한에 걸린다.

## 4. 개선안

| 안 | 바꾼 것 | 판단 |
|---|---|---|
| 안 1 `1-select-after-commit` | 응답용 SELECT를 COMMIT 뒤로 옮긴다. 2줄 추가 | **탈락.** 잠금 중 왕복이 2로 줄 뿐이다. 설정 20ms · 50%에서 FAIL(5xx 72건, p95 5.2초) |
| 안 2 `2-cte-autocommit` | 잠금 · INSERT를 한 문장 CTE로 묶고 자동 커밋 | 쓰기는 해결. 읽기 3왕복이 남아 설정 40ms · 50% FAIL |
| 안 3 `3-pipeline` | 같은 문장들을 파이프라인 한 묶음으로 자동 커밋 | 쓰기는 해결. 읽기 3왕복이 남아 설정 40ms · 50% FAIL |
| 안 2+읽기 `2r-cte-autocommit-reads` | 안 2 + `list_messages` 자동 커밋(BEGIN · COMMIT 왕복 제거) | 후보. 측정은 안 3+읽기와 같다 |
| **안 3+읽기 `3r-pipeline-reads`** | 안 3 + `list_messages` 자동 커밋 | **추천** |
| 비교용 `3b-pipeline-begin` | 파이프라인이되 BEGIN을 쓴다 | 3왕복이 되어 자동 커밋 방식으로 바꿨다(psycopg 3.3.6은 BEGIN에서 sync하고 나올 때 Sync를 또 보낸다) |

**잠금은 어느 안에서도 없애지 않았다.** 전파가 "마지막 id 이후"를 조회하므로 커밋 순서와 id 순서가 어긋나면 메시지를 건너뛴다(5절 음성 대조).

**안 3+읽기를 고른 이유는 측정값이 아니라 코드 위험이다.** 측정은 안 2+읽기와 차이가 나지 않는다.

| | 안 3+읽기 | 안 2+읽기 |
|---|---|---|
| 재전송 · 충돌 | 1왕복 | 2왕복 |
| 시간 제한 | `statement_timeout` 그대로 | `lock_timeout`으로 바뀐다. 한 문장 안에서는 `statement_timeout`을 바꿔도 듣지 않아서다. INSERT 자체에는 제한이 없다 |
| 잠금과 id 순서 | 문장 순서로 보장된다 | INSERT가 잠금 CTE의 행을 읽어야만 보장된다. SQL 모양만 달라도 깨지고(`control-cte-late-lock`) demo-chat 자체 시험은 못 잡는다 |
| 기존 코드와 차이 | 잠금 · `set_config` · INSERT 문장을 그대로 쓰고, 응답용 SELECT만 키로 찾는다 | INSERT를 CTE로 다시 쓴다 |
| 의존 | psycopg 파이프라인 + 자동 커밋 동작 | SQL만. **이 점은 안 2가 낫다** |
| 설정 0ms 처리량 | 3,353 tx/s | 안 2 5,976 tx/s(읽기 수정 전). 설정 5ms부터는 차이가 없다 |

## 5. 불변식

**모든 개선안이 통과했다.** 16과 18.4, 직접과 PgBouncer transaction 모드의 4조합을 안마다 돌렸다. 조합마다 검사 29개다. `invariants.json`.

| 안 | 조합 | 검사 통과 | 뒤집힘(최대) | 건너뜀(최대) | 커밋 뒤 응답 유실(최소) | demo-chat 자체 시험 |
|---|---:|---:|---:|---:|---:|---|
| 원본 | 4 | 116/116 | 0 | 0 | 13 | 24 passed |
| 안 1 | 4 | 116/116 | 0 | 0 | 30 | 24 passed |
| 안 2 | 4 | 116/116 | 0 | 0 | 13 | 24 passed |
| 안 3 | 4 | 116/116 | 0 | 0 | 15 | 24 passed |
| 안 2+읽기 | 4 | 116/116 | 0 | 0 | 13 | 24 passed |
| 안 3+읽기 | 4 | 116/116 | 0 | 0 | 15 | 24 passed |
| cte-late-lock | 4 | 104/116 | 40 | 14 | 13 | 24 passed |
| no-lock | 4 | 100/116 | 176 | 73 | 7 | 24 passed |

| 불변식 | 어떻게 보나 |
|---|---|
| 커밋 순서 = id 순서 | 논리 디코딩(`test_decoding` 슬롯)으로 실제 커밋 순서를 읽어 뒤집힘을 센다 |
| 전파가 건너뛰지 않음 | 앱과 같은 `list_messages` 조회 4개를 쓰기와 함께 돌려 놓친 id를 센다 |
| 멱등 재전송 · 충돌 | 같은 키 재전송은 같은 행과 같은 응답, 내용이 다르면 충돌. 동시에 같은 키 20건을 보내도 행은 1개 |
| 없는 첨부 | `UnknownAttachment`가 나고, 행이 남지 않고, 잠금이 풀리고, 같은 연결로 다시 보내면 저장된다 |
| 시간 제한 | 잠금 대기가 제한 안에 포기되고 `OperationalError` 계열이 나온다(앱이 503으로 바꾼다) |
| PgBouncer transaction 모드 | 위 전부를 PgBouncer 경유로 돌린다. `statement_timeout` · `lock_timeout` · 자동 커밋 설정이 세션에 남지 않는지 본다 |
| 연결 끊김 | 8% 확률로 연결을 끊어 "커밋 뒤 응답 유실"이 실제로 일어나게 하고(표의 건수), 재시도 뒤 키마다 한 행인지 본다 |

**음성 대조로 시험이 실제로 잡는지 확인했다.** `control-no-lock`(잠금 삭제)과 `control-cte-late-lock`(잠금을 id 발급 뒤로 민 CTE)은 뒤집힘과 건너뜀이 나와 탈락한다.

**demo-chat 자체 시험은 두 음성 대조에서도 모두 통과한다**(24 passed). 지연이 없으면 순서가 잘 어긋나지 않기 때문이다. 잠금을 지워도 CI가 못 잡는다는 뜻이다. 순서를 직접 만드는 시험을 `patches/test-lock-order.patch`로 제안한다. 음성 대조만 실패하고 나머지는 통과한다. `order-test.json`.

| 안 | 결과 | 실패 지점 |
|---|---|---|
| 원본 | 4 passed | - |
| 안 1 | 4 passed | - |
| 안 2 | 4 passed | - |
| 안 2+읽기 | 4 passed | - |
| 안 3 | 4 passed | - |
| 안 3(BEGIN) | 4 passed | - |
| 안 3+읽기 | 4 passed | - |
| 음성 대조 잠금 삭제 | 1 failed, 3 passed | AssertionError: 잠금을 쥔 동안 쓰기가 끝났다 |
| 음성 대조 잠금을 뒤로 민 CTE | 1 failed, 3 passed | AssertionError: 잠금을 잡기 전에 id를 받았다 |

## 6. 행렬: 직접 경로(같은 조건, 2회)

칸은 `판정<br>5xx<br>쓰기 p95`이고 회차를 모두 적는다. **5xx는 쓰기와 읽기를 더한 수다**(#158의 1,526건은 쓰기만). 부하가 끝난 뒤 재시도만 담긴 마지막 창의 UNKNOWN은 판정에서 뺐다. `matrix-direct.jsonl`.

| 설정 왕복 | 실제 왕복(부하 중) | 목적지 | 원본 | 안 1 | 안 2 | 안 3 | 안 2+읽기 | 안 3+읽기 |
|---:|---:|---:|---|---|---|---|---|---|
| 0ms | 0.6~0.8ms | 10% | PASS<br>5xx 0 / 0<br>p95 7ms / 7ms | PASS<br>5xx 0 / 0<br>p95 7ms / 7ms | PASS<br>5xx 0 / 0<br>p95 5ms / 5ms | PASS<br>5xx 0 / 0<br>p95 5ms / 5ms | PASS<br>5xx 0 / 0<br>p95 5ms / 5ms | PASS<br>5xx 0 / 0<br>p95 5ms / 5ms |
| 0ms | 0.6~0.7ms | 50% | PASS<br>5xx 0 / 0<br>p95 7ms / 7ms | PASS<br>5xx 0 / 0<br>p95 9ms / 10ms | PASS<br>5xx 0 / 0<br>p95 5ms / 5ms | PASS<br>5xx 0 / 0<br>p95 5ms / 5ms | PASS<br>5xx 0 / 0<br>p95 5ms / 5ms | PASS<br>5xx 0 / 0<br>p95 5ms / 5ms |
| 5ms | 6.5~6.9ms | 10% | PASS<br>5xx 0 / 0<br>p95 42ms / 43ms | FAIL / PASS<br>5xx 165 / 0<br>p95 51ms / 55ms | PASS<br>5xx 0 / 0<br>p95 10ms / 10ms | PASS<br>5xx 0 / 0<br>p95 10ms / 10ms | PASS<br>5xx 0 / 0<br>p95 11ms / 11ms | PASS<br>5xx 0 / 0<br>p95 11ms / 10ms |
| 5ms | 6.0~6.6ms | 50% | PASS<br>5xx 0 / 0<br>p95 43ms / 42ms | PASS<br>5xx 0 / 0<br>p95 55ms / 55ms | PASS<br>5xx 0 / 0<br>p95 11ms / 11ms | PASS<br>5xx 0 / 0<br>p95 11ms / 11ms | PASS<br>5xx 0 / 0<br>p95 11ms / 12ms | PASS<br>5xx 0 / 0<br>p95 11ms / 11ms |
| 20ms | 21.9~23.1ms | 10% | PASS<br>5xx 3 / 0<br>p95 142ms / 142ms | PASS<br>5xx 0 / 0<br>p95 182ms / 182ms | PASS<br>5xx 0 / 0<br>p95 26ms / 26ms | PASS<br>5xx 0 / 0<br>p95 26ms / 27ms | PASS<br>5xx 0 / 0<br>p95 27ms / 27ms | PASS<br>5xx 0 / 0<br>p95 26ms / 26ms |
| 20ms | 21.1~21.7ms | 50% | FAIL<br>5xx 1,816 / 1,496<br>p95 5.3s / 5.3s | FAIL<br>5xx 72 / 72<br>p95 5.2s / 5.2s | PASS<br>5xx 0 / 0<br>p95 26ms / 27ms | PASS<br>5xx 0 / 0<br>p95 26ms / 26ms | PASS<br>5xx 0 / 0<br>p95 27ms / 27ms | PASS<br>5xx 0 / 0<br>p95 27ms / 28ms |
| 40ms | 41.5~43.0ms | 10% | FAIL<br>5xx 99 / 176<br>p95 280ms / 323ms | PASS<br>5xx 4 / 14<br>p95 341ms / 345ms | PASS<br>5xx 0 / 0<br>p95 46ms / 45ms | PASS<br>5xx 0 / 0<br>p95 46ms / 45ms | PASS<br>5xx 0 / 0<br>p95 46ms / 46ms | PASS<br>5xx 0 / 0<br>p95 46ms / 45ms |
| 40ms | 41.4~42.5ms | 50% | FAIL<br>5xx 4,633 / 4,971<br>p95 5.4s / 5.4s | FAIL<br>5xx 2,278 / 2,351<br>p95 5.5s / 5.5s | FAIL<br>5xx 0 / 0<br>p95 2.7s / 1.8s | FAIL<br>5xx 0 / 0<br>p95 2.3s / 2.4s | PASS<br>5xx 0 / 0<br>p95 47ms / 47ms | PASS<br>5xx 0 / 0<br>p95 47ms / 46ms |

| 안 | 설정 왕복 | 목적지 | 회차 | 실패 | 유실 | 중복 | 내용 불일치 | 유령 행 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 원본 | 40ms | 50% | 1 | 9 | 0 | 0 | 0 | 0 |
| 원본 | 40ms | 50% | 2 | 9 | 0 | 0 | 0 | 0 |

- 안 1 설정 5ms · 10%의 1회차 FAIL은 Edge 대역의 결함이다(10절). 3회 중 1회만 나왔다.
- 안 2 · 안 3의 설정 40ms · 50%는 5xx가 0인데 p95로 FAIL이다. 잠금이 아니라 읽기의 BEGIN · SELECT · COMMIT 3왕복이 풀을 차지해서다. 읽기를 고치면 p95가 47ms가 된다.

## 7. 행렬: PgBouncer 경로(2회)

실제 구성과 같이 `앱 → PgBouncer → (지연) → DB`로 놓았다. 출발지 쪽 PgBouncer에는 지연이 없다. 결과는 직접 경로와 같은 모양이다. `matrix-pgbouncer.jsonl`.

| 설정 왕복 | 실제 왕복(부하 중) | 목적지 | 원본 | 안 2+읽기 | 안 3+읽기 |
|---:|---:|---:|---|---|---|
| 0ms | 0.6~0.7ms | 10% | PASS<br>5xx 0 / 0<br>p95 8ms / 8ms | PASS<br>5xx 0 / 0<br>p95 6ms / 6ms | PASS<br>5xx 0 / 0<br>p95 6ms / 6ms |
| 0ms | 0.5~0.7ms | 50% | PASS<br>5xx 0 / 0<br>p95 8ms / 8ms | PASS<br>5xx 0 / 0<br>p95 6ms / 6ms | PASS<br>5xx 0 / 0<br>p95 6ms / 6ms |
| 5ms | 6.5~6.8ms | 10% | PASS<br>5xx 0 / 0<br>p95 45ms / 44ms | PASS<br>5xx 0 / 0<br>p95 11ms / 11ms | PASS<br>5xx 0 / 0<br>p95 11ms / 11ms |
| 5ms | 6.2~6.5ms | 50% | PASS<br>5xx 0 / 0<br>p95 45ms / 45ms | PASS<br>5xx 0 / 0<br>p95 12ms / 12ms | PASS<br>5xx 0 / 0<br>p95 12ms / 12ms |
| 20ms | 21.9~23.1ms | 10% | PASS<br>5xx 0 / 0<br>p95 146ms / 144ms | PASS<br>5xx 0 / 0<br>p95 27ms / 27ms | PASS<br>5xx 0 / 0<br>p95 27ms / 27ms |
| 20ms | 21.4~22.2ms | 50% | FAIL<br>5xx 1,959 / 1,635<br>p95 5.3s / 5.3s | PASS<br>5xx 0 / 0<br>p95 28ms / 28ms | PASS<br>5xx 0 / 0<br>p95 28ms / 28ms |
| 40ms | 41.7~42.8ms | 10% | FAIL<br>5xx 111 / 202<br>p95 293ms / 342ms | PASS<br>5xx 0 / 0<br>p95 46ms / 47ms | PASS<br>5xx 0 / 0<br>p95 46ms / 47ms |
| 40ms | 41.5~42.2ms | 50% | FAIL<br>5xx 4,996 / 5,576<br>p95 5.4s / 5.4s | PASS<br>5xx 0 / 0<br>p95 47ms / 47ms | PASS<br>5xx 0 / 0<br>p95 47ms / 47ms |

| 안 | 설정 왕복 | 목적지 | 회차 | 실패 | 유실 | 중복 | 내용 불일치 | 유령 행 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 원본 | 40ms | 50% | 1 | 16 | 0 | 0 | 0 | 0 |
| 원본 | 40ms | 50% | 2 | 50 | 0 | 0 | 0 | 0 |

## 8. 포화 경계

경계는 다음과 같이 움직였다. `matrix-boundary.jsonl`에 사이 칸이 있고, 칸마다 1~6회다.

| 안 | 목적지 | 마지막 PASS(실제 왕복) | 처음 FAIL(실제 왕복) | 회차별 결과 |
|---|---:|---:|---:|---|
| 원본 | 10% | 30ms(32.1ms) | 35ms(37.0ms) | 0ms PP · 5ms PP · 20ms PP · 25ms P · 30ms PP · 35ms FF · 40ms FF |
| 원본 | 50% | 10ms(11.1ms) | 12ms(13.4ms) | 0ms PP · 5ms PP · 8ms P · 10ms PP · 12ms FF · 15ms F · 20ms FF · 40ms FF |
| 안 1 | 10% | 40ms(41.6ms) | 60ms(62.0ms) | 0ms PP · 5ms EPP · 20ms PP · 40ms PP · 60ms FF · 80ms F |
| 안 1 | 50% | 15ms(16.3ms) | 20ms(21.4ms) | 0ms PP · 5ms PP · 8ms P · 10ms P · 12ms P · 15ms PP · 20ms FF · 40ms FF |
| 안 2 | 10% | 40ms(42.2ms) | 없음 | 0ms PP · 5ms PP · 20ms PP · 40ms PP |
| 안 2 | 50% | 20ms(21.2ms) | 40ms(42.2ms) | 0ms PP · 5ms PP · 20ms PP · 40ms FF |
| 안 3 | 10% | 40ms(42.0ms) | 없음 | 0ms PP · 5ms PP · 20ms PP · 40ms PP |
| 안 3 | 50% | 35ms(37.2ms) | 40ms(41.9ms) | 0ms PP · 5ms PP · 20ms PP · 25ms P · 30ms P · 35ms PP · 40ms FF |
| 안 2+읽기 | 10% | 40ms(42.7ms) | 없음 | 0ms PP · 5ms PP · 20ms PP · 40ms PP |
| 안 2+읽기 | 50% | 80ms(81.8ms) | 100ms(102.5ms) | 0ms PP · 5ms PP · 20ms PP · 40ms PP · 60ms ESSPPP · 80ms SSP · 100ms SFF · 120ms F |
| 안 3+읽기 | 10% | 200ms(203.0ms) | 300ms(302.7ms) | 0ms PP · 5ms PP · 20ms PP · 40ms PP · 150ms P · 200ms PP · 300ms FF · 400ms F |
| 안 3+읽기 | 50% | 80ms(81.6ms) | 100ms(102.3ms) | 0ms PP · 5ms PP · 20ms PP · 40ms PP · 60ms PPPPP · 80ms SPP · 100ms FF · 120ms F · 150ms F |

경계를 재기 전에 적어 둔 예측과 견주면 다음과 같다.

| 안 · 목적지 | 계산 | 예측 왕복 | 측정(PASS / FAIL) |
|---|---|---:|---:|
| 원본 50% | 쓰기 25건/s × 3왕복 ≤ 1초 | 약 13ms | 11.1 / 13.4ms |
| 안 1 50% | 쓰기 25건/s × 2왕복 ≤ 1초 | 약 20ms | 16.3 / 21.4ms |
| 안 3 50% | (읽기 75건 × 3 + 쓰기 25건 × 1)왕복 ≤ 풀 10 | 약 40ms | 37.2 / 41.9ms |
| 안 3+읽기 50% | 100건 × 1왕복 ≤ 풀 10 | 약 100ms | 81.6 / 102.3ms |
| 안 3+읽기 10% | 20건 × 1왕복 ≤ 풀 10 | 약 500ms | 203 / 303ms |

- 50%의 네 줄은 예측과 맞는다. 포화 지점이 원인 설명대로 움직였다는 뜻이다. 안 3+읽기의 81.6ms는 3회 중 1회(멈춤)를 뺀 값이다.
- 안 3+읽기 10%는 예측보다 일찍 FAIL한다. 설정 300ms의 FAIL은 **첫 창만**이다(풀이 연결을 여는 구간). 400ms는 모든 창이 FAIL인데 원인은 더 보지 않았다. 판단에 쓸 왕복 범위 밖이다.
- 원본과 안 1의 10% 경계(실제 37ms, 62ms)는 잠금 포화보다 먼저 온다. 200ms 대기 제한에 걸려 오류율로 FAIL한다. 이 해석은 측정 뒤에 한 것이다.

## 9. PgBouncer PAUSE

부하 중 15초 시점에 PAUSE를 걸어 끝날 때까지 시간을 쟀다. 전환 때 쓰기를 멈추는 동작이다. 기준은 executor `pause_timeout_s` 기본 3초와 demo-chat의 "최대 트랜잭션 200ms 이하"다. `matrix-pause.jsonl`.

| 설정 왕복 | 목적지 | 안 | 출발지 PAUSE | 목적지 PAUSE | judge |
|---:|---:|---|---:|---:|---|
| 20ms | 50% | 원본 | 157.9ms | 249.4ms | FAIL |
| 20ms | 50% | 원본 | 142.0ms | 231.2ms | FAIL |
| 20ms | 50% | 안 1 | 110.0ms | 258.7ms | FAIL |
| 20ms | 50% | 안 1 | 95.6ms | 155.0ms | FAIL |
| 20ms | 50% | 안 2+읽기 | 0.6ms | 19.8ms | PASS |
| 20ms | 50% | 안 2+읽기 | 0.6ms | 18.9ms | PASS |
| 20ms | 50% | 안 3+읽기 | 0.5ms | 16.5ms | PASS |
| 20ms | 50% | 안 3+읽기 | 0.4ms | 18.6ms | PASS |
| 40ms | 50% | 원본 | 195.5ms | 344.3ms | FAIL |
| 40ms | 50% | 원본 | 127.7ms | 286.1ms | FAIL |
| 40ms | 50% | 안 1 | 63.9ms | 251.1ms | FAIL |
| 40ms | 50% | 안 1 | 82.0ms | 271.1ms | FAIL |
| 40ms | 50% | 안 2+읽기 | 0.6ms | 34.3ms | PASS |
| 40ms | 50% | 안 2+읽기 | 0.4ms | 40.6ms | PASS |
| 40ms | 50% | 안 3+읽기 | 0.5ms | 36.1ms | PASS |
| 40ms | 50% | 안 3+읽기 | 0.5ms | 21.4ms | PASS |

3초 안에는 모두 들어온다. 원본과 안 1의 목적지 PAUSE는 200ms를 넘기는 칸이 있고, 안 3+읽기는 설정 40ms에서도 36ms 이하다. 출발지 PAUSE는 1ms 이하가 된다.

## 10. 경계 계산에서 뺀 회차: Edge 대역의 502와 원인 미확인 멈춤

전체 215회차 중 아래 회차를 경계 계산에서 뺐다. E는 Edge 대역이 만든 502, S는 원인을 확인하지 못한 멈춤이다. 행렬 표(6 · 7절)의 원래 칸에는 그대로 둔다.

| 구분 | 안 | 설정 왕복 | 목적지 | 회차 | 5xx | 시간 초과 | 쓰기 p95 |
|---|---|---:|---:|---:|---:|---:|---:|
| E | 안 2+읽기 | 60ms | 50% | 2 | 243 | 0 | 70ms |
| E | 안 1 | 5ms | 10% | 1 | 165 | 0 | 51ms |
| S | 안 2+읽기 | 60ms | 50% | 3 | 0 | 779 | 15.0s |
| S | 안 2+읽기 | 60ms | 50% | 4 | 0 | 87 | 12.5s |
| S | 안 2+읽기 | 80ms | 50% | 1 | 0 | 1,627 | 15.2s |
| S | 안 2+읽기 | 80ms | 50% | 2 | 61 | 894 | 15.1s |
| S | 안 2+읽기 | 100ms | 50% | 1 | 0 | 502 | 15.0s |
| S | 안 3+읽기 | 80ms | 50% | 1 | 0 | 1,154 | 15.1s |

**E: Edge 대역이 만든 502.** 앱은 오류를 남기지 않았고 응답에 환경 헤더가 없다.

- 로그가 있는 2칸(안 2+읽기 60ms 2회차, 80ms 2회차. 뒤의 칸은 S와 겹쳐 표에 S로 적었다)에서 `upstream prematurely closed connection` 1건 뒤에 `no live upstreams`가 243건, 62건 이어졌다.
- `host.docker.internal`이 IPv4와 IPv6 두 주소로 풀리고 IPv6는 항상 실패해 꺼져 있다. 그래서 IPv4가 한 번만 실패해도 살아 있는 서버가 없어 nginx가 바로 502를 낸다.
- 앱을 0.5초 죽이는 시험에서 같은 동작을 재현했다. 앱이 끊긴 시간은 0.5초인데, 그 동안뿐 아니라 약 7초 뒤에 다시 502가 이어져 3.02~12.59초 사이 50건이 나왔다. `edge_blip.py`, `edge-blip.txt`.
- 안 1 칸은 로그가 없어 **단정하지 못한다.** 즉시 거절 · 헤더 없음 · 앱 오류 0은 같지만, 502가 10.2초 연속이라 시간 모양은 재현 시험과 다르다.
- 실제 Cloudflare의 동작이 아니다. 첫 연결 끊김의 원인은 확인하지 않았다. uvicorn 기본 keep-alive 5초와의 경합을 의심한다.

**S: 원인 미확인 멈춤.** 모두 설정 60~100ms 칸이다. 응답이 지연 없는 출발지까지 함께 느려지고 15초 시간 초과가 난다. **원인을 확인하지 못했으므로 측정 환경의 결함이라고 단정하지 않는다.**

- 안 3+읽기 80ms 1회차는 5.5초 시점까지 목적지 대기가 줄고 있었는데(1,029ms → 488ms), 갑자기 지연 없는 출발지까지 3ms에서 수 초로 뛰고 15초 시간 초과가 나왔다. `stall-timeline.txt`.
- 그 칸의 DB 왕복 측정은 내내 최대 86ms였고 5xx와 앱 오류도 0이다. 발생기 · Edge 대역 · Docker 네트워크 쪽을 의심하지만 확인하지 못했다. 앱이 원인이 아니라고 배제하지도 못했다.
- #158 증거 2절의 "15초 멈춤"과 모양이 같다.
- 안 2+읽기에 더 많이 나와 안의 차이인지 확인했다. **두 안을 설정 60ms · 50%에서 번갈아 3회씩 돌리면 6회 모두 PASS다.** 멈춤은 05:42~05:49와 06:53~07:07에 몰려 있어 시간대의 영향일 수 있다. 안의 차이가 없다고 단정할 만큼 많이 돌리지는 않았다.
- 설정 40ms 이하에서는 나오지 않았다(181회차).
- 탐색 실행 1회에서 원본 설정 40ms · 50%가 p95 15초, 실패 347건이었다. 발생기 지연 p95 720ms, 시작 부하 5.75였다. 같은 종류로 보고 표에서 뺐다. 그 원자료는 이 폴더에 없다. 다른 작업과 겹치면 표보다 나빠질 수 있다.

**Edge 오류 집계.** 결과 행의 `edge_errors`는 처음에 긴 로그 줄을 놓쳐 0으로 나왔다. 이 필드는 결과 파일에서 뺐고, 남아 있는 로그 121개에서 다시 센 값을 `edge-log-summary.json`에 두었다. 직접 경로 행렬(`matrix-direct.jsonl`)은 그 전에 재서 로그가 없다. 끊긴 IPv6 연결(5,803건)은 nginx가 IPv4로 넘기므로 응답에 영향이 없다.

## 11. 추천안 종단 회귀

안 3+읽기 패치본으로 #158의 종단 시나리오 중 앱 쓰기 경로에 닿는 6개를 돌렸다. **검사 31건, 실패 0건.** `e2e-pipeline-reads.json`.

| 설정 왕복 | 목적지 | 쓰기 p95 | 쓰기 5xx | judge | 실패 / 유실 / 중복 |
|---:|---:|---:|---:|---|---:|
| 0ms | 10% | 5ms | 0 | PASS PASS PASS | 0 / 0 / 0 |
| 0ms | 50% | 5ms | 0 | PASS PASS PASS | 0 / 0 / 0 |
| 5ms | 10% | 11ms | 0 | PASS PASS PASS | 0 / 0 / 0 |
| 5ms | 50% | 12ms | 0 | PASS PASS PASS | 0 / 0 / 0 |
| 20ms | 10% | 27ms | 0 | PASS PASS PASS | 0 / 0 / 0 |
| 20ms | 50% | 27ms | 0 | PASS PASS PASS | 0 / 0 / 0 |
| 40ms | 10% | 45ms | 0 | PASS PASS PASS | 0 / 0 / 0 |
| 40ms | 50% | 47ms | 0 | PASS PASS PASS | 0 / 0 / 0 |

시나리오별 통과: `ack-loss` 4/4, `write-block` 5/5, `db-pause` 4/4, `failed-but-stored` 3/3, `tampering` 7/7, `lock-rtt` 8/8.

돌리지 않은 시나리오는 `direct`, `mock-edge`, `all-failed`, `edge-error`, `window-breaks`, `soak`이다. `lock-rtt`는 합격선을 정하지 않는 시나리오라, 위 표의 judge 값을 원본의 값(6절)과 견주어 읽는다.

## 12. 재지 않은 것

- **실제 클라우드 사이 왕복, 지터, 패킷 손실, Tailscale 비용.** 고정 지연만 넣었다. 실제 값은 없다.
- 실제 Cloudflare, 실제 PgBouncer TLS, 실제 앱 컨테이너와 복수 프로세스. 앱은 uvicorn 1개, PgBouncer 경로는 평문이다.
- 실제 전파 · 복제 구성. 건너뛰기는 앱과 같은 조회로 흉내 냈고, 실제 전파기를 돌리지 않았다.
- 행렬의 Postgres 18.4. 행렬은 16으로 재고 18.4는 불변식만 돌렸다.
- 첨부 경로의 부하. 부하는 `/api/messages`만 친다. `add_attachment` · `get_attachment`는 고치지 않아 여전히 여러 왕복을 쓴다.
- 오래 도는 부하. 칸마다 30초, 회차도 1~3회다. 표의 차이를 통계로 검정하지 않았다.
- 메시지가 많이 쌓인 DB. 시험마다 새 DB다.
- demo-chat PR #14를 합친 앱. 측정은 main `dc9bece` 기준이다(13절).
- judge 기준 확정값. `p95_ms_max=1500`은 자리값이라 바뀌면 경계도 바뀐다.

## 13. 담당자와 논의할 것

1. **실제 왕복(T8).** 약 11ms 이하면 원본도 버틴다. 그보다 크면 패치 없이는 50% 단계를 못 넘는다. 약 37ms부터는 10% 단계도 FAIL이다.
2. **패치 채택(T22).** `patches/3r-pipeline-reads.patch`는 `app/store.py` 한 파일이다. 자체 시험 24개와 불변식 검사를 통과했다. 안 2+읽기를 더 선호할 이유(파이프라인 의존 회피)가 있는지도 묻는다.
   **열려 있는 demo-chat PR #14(`app/main.py`의 `X-Bravo-Env` 등, head `7c821c3`)와 파일이 겹치지 않는다.** 그 PR은 `app/store.py`와 `tests/test_store.py`를 바꾸지 않고, psycopg 3.3.6 · psycopg-pool 3.3.3도 그대로다. 패치 3개(`1-`, `2r-`, `3r-`)와 `test-lock-order.patch`가 그 head에 `git apply --check`로 적용된다. 합친 앱으로 다시 재지는 않았다.
3. **시험 보강(T22).** `patches/test-lock-order.patch`. 지금 시험은 잠금을 지워도 통과한다.
4. **목적지 PgBouncer 설정(T17).** 측정 때는 저장소에 없었고, 지금은 `infra/envs/target-workloads/pgbouncer.ini`에 측정에 쓴 값과 같은 `default_pool_size=10`으로 있다. 이 값으로 충분한지 정해야 한다. 패치 뒤의 한계는 `연결 수 ÷ 왕복`이라 `default_pool_size`가 경계를 정한다. 앱 복제본을 늘려도 PgBouncer 연결 수가 그대로면 늘지 않는다.
5. **p95 기준 확정(T17).** 자리값 1500ms 기준으로 잰 경계다.

## 14. 재현

```sh
# demo-chat을 안 이름 폴더마다 clone하고 patches/의 패치를 적용한 뒤 uv sync 한다.
cd services/loadgen
uv run python testing/lock_lab.py wire --variants <폴더> --out <산출물> --names baseline select-after-commit cte-autocommit pipeline pipeline-begin cte-autocommit-reads pipeline-reads
uv run python testing/lock_lab.py ceiling --variants <폴더> --out <산출물> --names baseline select-after-commit cte-autocommit pipeline pipeline-begin pipeline-reads --rtts 0 5 10 20 40 --duration 8
uv run python testing/lock_lab.py invariants --variants <폴더> --out <산출물> --names baseline select-after-commit cte-autocommit pipeline cte-autocommit-reads pipeline-reads no-lock cte-late-lock
uv run python testing/lock_lab.py matrix --variants <폴더> --out <산출물> --label direct --names baseline select-after-commit cte-autocommit pipeline cte-autocommit-reads pipeline-reads --reps 2
uv run python testing/lock_lab.py matrix --variants <폴더> --out <산출물> --label bouncer --path pgbouncer --names baseline cte-autocommit-reads pipeline-reads --reps 2
uv run python testing/lock_lab.py matrix --variants <폴더> --out <산출물> --label pause --path pgbouncer --pause-at 15 --names baseline select-after-commit cte-autocommit-reads pipeline-reads --rtts 20 40 --percents 50 --reps 2
uv run python testing/lock_lab_report.py <이 폴더>/matrix-direct.jsonl                    # 6절 표
uv run python testing/lock_lab_report.py <이 폴더>/matrix-direct.jsonl <이 폴더>/matrix-boundary.jsonl --boundary   # 8절 표
uv run python testing/e2e.py --demo-chat <폴더>/pipeline-reads --out <산출물> --container-prefix lock-lab --only ack-loss write-block db-pause failed-but-stored tampering lock-rtt   # 11절
python3 <이 폴더>/edge_blip.py <산출물>   # 10절
```

표의 숫자는 위 스크립트가 결과 파일에서 뽑았다. 원자료(journal, 앱 로그)는 크기 때문에 넣지 않았다. Docker가 필요하고 클라우드에 접속하지 않는다. 컨테이너는 `lock-lab-` 접두사로 띄우고 끝나면 지운다.
