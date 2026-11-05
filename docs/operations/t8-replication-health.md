# T8 정방향 복제 상태: 꺼짐 원인 확인 · 설정 분리 · 지표

**구독이 꺼지면 아래 한 줄로 꺼진 시각과 오류 종류를 값 노출 없이 읽을 수 있다.** 정방향 `disable_on_error`는 이제 역방향과 따로 렌더할 수 있고, 목적지 구독 상태와 적용 오류 수가 CNPG 지표로 나온다. 렌더 기본값은 양방향 모두 그대로 `true`이고, 실제 클러스터에는 아무것도 적용하지 않았다.

이 문서의 숫자는 전부 임시 환경에서 잰 값이다([재지 못한 것](#재지-못한-것)). 원값은 [측정 기록](../evidence/t8-replication-health-2026-11-06/README.md)에 있다.

## 꺼졌을 때 원인 확인

읽기만 한다. ENABLE이나 시험 행 쓰기를 하지 않으며, 지난 복구 승인을 대신하지도 않는다. 목적지에 `kubectl` 권한이 있으면 접속 정보 없이 시각과 오류 종류까지 나온다.

```bash
kubectl -n target-dev logs target-db-1 -c postgres --since=48h | python3 infra/envs/target-workloads/replication_incident.py
```

두 DB 접속 정보가 있으면 WAL 차이와 오류 수까지 한 번에 나온다. 값은 `resume_replication.py`와 같은 변수로 주며 `verify-full`이어야 한다. 역할은 `chat_replicator`면 된다.

```bash
kubectl -n target-dev logs target-db-1 -c postgres --since=48h | infra/envs/target-workloads/replication_incident.py
```

접속 정보가 없거나 한쪽 DB에 닿지 못하면 읽은 부분만 나오고 종료 코드가 1이다. 끊김 중에는 출발지가 닿지 않을 수 있는데, 그래도 목적지 값과 로그 판정은 나온다(`state.errors`에 실패한 쪽의 분류만 적힌다). 출발지 `kubectl`이 있으면 WAL 차이는 따로 읽는다.

```bash
kubectl -n source-dev exec source-db-1 -c postgres -- psql -X -q -At -d chat -c "BEGIN READ ONLY; SELECT active, wal_status, pg_wal_lsn_diff(pg_current_wal_flush_lsn(), confirmed_flush_lsn) FROM pg_replication_slots WHERE slot_name = 't8_source_to_target'; ROLLBACK;"
```

| 출력 | 뜻 |
| --- | --- |
| `disables[].at` | 꺼진 시각. 카탈로그에는 시각이 남지 않아 로그에서 읽는다 |
| `disables[].cause` | `connection`(타임아웃 · 스트림 끊김), `conflict`(무결성 충돌), `other`, `unknown`(같은 프로세스의 오류가 로그 범위 안에 없다) |
| `disables[].event`, `sqlstate` | 고정된 분류 이름과 SQLSTATE. 로그 문장은 출력하지 않는다 |
| `latest_error`, `counts` | 꺼지지 않고 재시도 중일 때의 마지막 오류와 분류별 건수 |
| `log_window` | 읽은 로그의 범위. **`disables`가 비어 있어도 이 범위 밖의 일은 모른다** |
| `state.gap_bytes`, `gap_mib` | 출발지 WAL과 슬롯 확인 위치의 차이. 접속 정보가 있을 때만 |
| `state.forward_enabled`, `apply_workers`, `target_apply_errors`, `target_conflicts` | 지금 상태와 누적 수 |

저장소가 없는 VM이면 왼쪽만 원격으로 돌린다: `ssh <VM> "kubectl -n target-dev logs target-db-1 -c postgres --since=48h" | python3 infra/envs/target-workloads/replication_incident.py`. 시각은 로그 그대로 UTC다.

`connection`은 그 시각에 연결이 끊겼다는 뜻까지만이다. 왜 끊겼는지(Tailscale 경로, 노드 재시작 등)는 이 명령이 말해 주지 못한다. Pod가 재시작했다면 `--previous`로 이전 컨테이너 로그도 읽는다. `conflict`면 다시 켜도 또 실패하므로 쓰기 경로부터 본다.

## 방향별 `disable_on_error`

구독이 있는 쪽을 `--side`로 고른다. `--side target`이 정방향, `--side source`가 역방향이다. 옵션을 주지 않으면 출력은 이전과 바이트까지 같고, 이를 시험이 고정한다. 역방향 안전장치와 타임아웃은 바꾸지 않았다.

```bash
python3 infra/envs/target-workloads/replication.py subscription --side target --disable-on-error false
```

끄면 끊김에서 스스로 회복하는 대신 "꺼짐" 신호가 사라지고, **충돌에는 끝없이 재시도한다.** 그래서 지표와 알람을 먼저 올리고 나서 끈다.

## 이미 떠 있는 구독 바꾸기

**구독이 켜져 있으면 `Subscription` 객체만 고친다. 꺼져 있으면 SQL과 객체를 둘 다 고친다.** 실제 구독은 CNPG 객체가 만들었고 소유자는 `postgres`다([기록](../evidence/2026-11-03-t8-development/readback.json)). 실제 환경에서는 실행하지 않았다. 아래 값은 임시 k3s의 실제 CNPG 1.30.1에서 잰 것이다.

| 한 일 | 결과 |
| --- | --- |
| 켜진 구독의 객체 변경 | 0.3초 만에 반영, 적용 워커 유지 |
| 꺼진 구독의 객체 변경 | 실패(`ALTER SUBSCRIPTION with refresh is not allowed for disabled subscriptions`). 값은 그대로이고, 켠 뒤 30.1초에 자동 반영 |
| 객체는 두고 SQL만 변경 | 70초 뒤에도 유지되지만, **다음 객체 변경 때 객체 값으로 덮인다** |

역방향은 평소 꺼져 있어 객체 변경이 실패 상태로 남는다. 바꾸지 않는다.

**1. 실행 전 확인.** 알람이 올라가 있고 실행 중인 컷오버가 없는지 본 뒤, 현재 값과 워커 PID를 적어 둔다.

```bash
kubectl -n target-dev exec target-db-1 -c postgres -- psql -X -q -At -d chat -c "BEGIN READ ONLY; SELECT subname, subenabled, subdisableonerr, (SELECT pid FROM pg_stat_subscription w WHERE w.subid = s.oid AND w.worker_type = 'apply') FROM pg_subscription s; ROLLBACK;"
```

```bash
kubectl -n target-dev get subscriptions.postgresql.cnpg.io t8-source-to-target -o jsonpath='{.spec.parameters.disable_on_error} {.status.applied}{"\n"}'
```

**2. 바꾸기.** 구독이 켜져 있으면 객체만 고친다.

```bash
kubectl -n target-dev patch subscriptions.postgresql.cnpg.io t8-source-to-target --type merge -p '{"spec":{"parameters":{"disable_on_error":"false"}}}'
```

구독이 꺼져 있으면 먼저 SQL로 바꾼 뒤 위 객체 변경도 한다. 객체는 켜질 때까지 `applied=false`로 남고 30초마다 다시 시도한다. **이 절차는 ENABLE을 하지 않는다.** 켜는 것은 별도 승인이다.

```bash
kubectl -n target-dev exec target-db-1 -c postgres -- psql -X -d chat -c 'ALTER SUBSCRIPTION t8_source_to_target SET (disable_on_error = false);'
```

**3. 실행 후 확인.** 1번을 다시 돌린다. `subdisableonerr`가 `f`이고, 켜져 있던 구독이면 PID가 같고 `applied`가 `true`여야 한다.

**4. 되돌리기.** 2번의 `false`를 `true`로 바꿔 같은 순서로 실행한다. 임시 PostgreSQL에서 켜진 구독에 끄기와 되돌리기를 3회 했고, 매번 워커가 유지됐다. 끈 동안은 8초 끊김에서 회복했고, 되돌린 뒤에는 같은 끊김에 다시 꺼졌다.

## 구독 상태 지표

`render.py database`에 ConfigMap `target-subscription-metrics`와 Cluster의 `monitoring` 블록을 더했다. 쿼리는 `pg_monitor` 권한인 `cnpg_metrics_exporter`로 실제 CNPG에서 돈다.

| 지표(라벨 `subname`) | 뜻 | 읽을 때 주의 |
| --- | --- | --- |
| `cnpg_pg_subscription_enabled` | 구독이 켜져 있으면 1 | 끄면 끊김에도 1로 남는다 |
| `cnpg_pg_subscription_apply_error_count` | 적용 오류 누적(counter) | 끊김도 1건 는다. 슬롯 점유 재시도(08P01)는 세지 않는다 |
| `cnpg_pg_subscription_apply_workers` | 존재하는 적용 워커 수 | **접속 중인 워커도 세므로 1이 복제 중이라는 뜻은 아니다.** 흐름은 출발지 슬롯 지연으로 본다 |

값은 CNPG 캐시 때문에 최대 30초 늦다(기본 `metricsQueriesTTL`, 실측 지연 최대 29.0초). 수집 주기 15초가 더해진다. 판정 입력이 아니며 컷오버 조건은 Executor가 DB에서 직접 읽는다(ADR-0010). 역방향이 있는 출발지(T6)에는 넣지 않았다.

떠 있는 클러스터에는 전체 렌더를 다시 적용하지 말고 이것만 더한다. 실제 CNPG에서 3.2초 뒤 지표가 나왔고 Pod와 적용 워커가 그대로였다. `infra/envs/target-workloads`에서 실행한다.

```bash
python3 -c 'import json, render; print(json.dumps(render.subscription_metrics()))' | kubectl apply -f -
```

```bash
kubectl -n target-dev patch cluster target-db --type merge -p '{"spec":{"monitoring":{"customQueriesConfigMap":[{"name":"target-subscription-metrics","key":"queries"}]}}}'
```

**수집 경로.** DB 지표 경로(`collector-db.yaml`)에는 이름 허용 목록이 없어(허용 목록은 앱 경로 전용) 새 지표를 막지 않는다. 다만 목적지 Collector에 `BRAVO_DB_NAMESPACE=target-dev`와 `collector-db-access`의 `target-dev` 적용이 필요하다. 저장소에서 `target-dev` DB Pod를 고르는 NetworkPolicy는 찾지 못했다. 실제 환경의 정책은 확인하지 못했다.

## 알람 (T37 담당과 합의 후 적용)

T37 파일이라 이 변경에 넣지 않았다. 규칙과 시험은 패치로 준비했고 `promtool` 시험을 통과했다.

| 알림 | 조건 | 왜 |
| --- | --- | --- |
| `BravoForwardSubscriptionOff` | 정방향 `enabled == 0`, 2분 | 지금 설정에서 복제가 멈춘 신호. 컷오버 뒤에는 일부러 끄므로 뜨는 것이 정상이다 |
| `BravoForwardSubscriptionError` | 오류 수 증가, 10분 창 | 끄면 이것이 첫 신호다. 끊김 한 번으로도 뜬다 |
| `BravoForwardSubscriptionRetrying` | 오류 5건 이상, 5분 창 | 충돌의 끝없는 재시도. 끊김은 1건, 충돌은 30초에 7건이라 갈린다 |
| `BravoForwardReplicationNotRecovering` | 정방향 슬롯 비활성, 2분 | 복구 지연. 측정한 회복은 모두 5.63초 이내였다 |

## 실측

대역이다. 임시 PostgreSQL 18.4 두 대, 가정한 netem 100Mbps · RTT 30ms, TLS 없음, 시드 데이터 없음, 쓰기 50 req/s(대기 조건 제외). 지표는 렌더한 쿼리를 `pg_monitor` 역할로 직접 돌린 값이다. 조건마다 3회.

| `disable_on_error` | 실패 | 횟수 | 꺼짐 | 회복 | 오류 증가(최소~최대) | 연결 복구 뒤 행 도착(중앙값 / 최대, 초) |
| --- | --- | --- | --- | --- | --- | --- |
| 켬 | 8초 끊김 | 3 | 3 | 0 | 1~1 | - |
| 켬 | 20초 끊김 | 3 | 3 | 0 | 1~1 | - |
| 켬 | 40초 끊김 | 3 | 3 | 0 | 1~1 | - |
| 켬 | 손실 30%, 30초 | 3 | 2 | 1 | 0~1 | 3.37 / 3.37 |
| 끔 | 8초 끊김 | 3 | 0 | 3 | 1~1 | 0.78 / 1.02 |
| 끔 | 20초 끊김 | 3 | 0 | 3 | 1~1 | 5.62 / 5.63 |
| 끔 | 40초 끊김 | 3 | 0 | 3 | 1~1 | 1.8 / 2.08 |
| 끔 | 손실 30%, 30초 | 3 | 0 | 3 | 0~1 | 2.42 / 2.81 |
| 켬 | 8초 끊김, 쓰기 없음 | 3 | 3 | 0 | 1~1 | - |
| 끔 | 8초 끊김, 쓰기 없음 | 3 | 0 | 3 | 1~1 | 1.02 / 1.04 |
| 켬 | 충돌 | 3 | 3 | 0 | 1~1 | - |
| 끔 | 충돌 | 3 | 0 | 3 | 7~7 | - |

꺼진 경우 지표는 `enabled` 0, `workers` 0, 오류 +1로 바뀌었다. 끈 경우 끊김 동안 `enabled`는 1을 유지했고 `workers`도 대부분 1이었다. 손실 30%는 기본값에서도 3회 중 1회 꺼지지 않아 경계에 있다. 충돌의 `회복`은 충돌 행을 지운 뒤의 값이고, 지우기 전 30초 동안은 재시도만 했다. 모든 시험 뒤 역방향은 OFF, `disable_on_error=true`로 그대로였다.

실제 CNPG(임시 k3s, 끊김은 walsender 정지로 대체, 1회)에서 같은 동작을 실제 exporter 페이지로 확인했다.

| 상황 | SQL | exporter에 보인 값 | 진단 명령 |
| --- | --- | --- | --- |
| 기본값 + 9초 정지 | 꺼짐, 오류 +1 | 19.3초 뒤 `enabled 0`, `workers 0` | `connection` / `receiver_timeout` / 08006 |
| 기본값 + 충돌 | 꺼짐, 오류 +1 | 29.0초 뒤 `enabled 0` | `conflict` / `apply_conflict` / 23505. 행 값은 원본 로그에 있고 출력에는 없음 |
| 끔 + 9초 정지 | 켜짐, 5.01초 뒤 행 도착, 오류 +1 | `enabled 1`, 오류 증가 | `disables` 비어 있음, `counts`에 타임아웃 1 |
| 끔 + 충돌 30초 | 켜짐, 오류 +8 | `enabled 1` 유지 | `latest_error`가 `conflict`, `disables` 비어 있음. 행을 지우자 3.9초에 회복 |

`verify-full` 접속 경로(포트 포워딩, `chat_replicator`)로 진단 명령을 돌려 `gap_bytes`를 읽었고 출력에 자격 증명은 없었다.

## 재지 못한 것

- **실제 환경 전부.** AWS · VM · Tailscale 경로에서 아무것도 돌리거나 적용하지 않았다. 11/5 23:50 사고의 원인은 아직 모른다. 진단 명령을 돌려야 나온다.
- **실제 끊김의 모양.** netem과 walsender 정지는 대역이다. DERP 전환이나 노드 재시작과 같다고 주장하지 않는다.
- **Collector → 저장소 → 알림의 실제 경로.** `promtool`은 합성 시계열이다. 저장소 이름 `cnpg_pg_subscription_apply_error_count_total`은 `cnpg_pg_stat_archiver_failed_count_total` 사례로 미뤄 본 것이다.
- **끈 상태의 실제 단절 컷오버.** Executor와 함께 돌리지 않았다(T12). 역방향 끄기는 재지 않았다.
- **오래 이어지는 재시도.** 슬롯이 `max_slot_wal_keep_size` 1GB에 닿는 경우, 40초를 넘는 끊김, 임계값 5건의 실제 오탐률.
- **로그 범위 밖.** 로그가 회전했거나 Pod가 바뀌면 `unknown`이나 빈 결과가 나온다. 출발지 로그는 분류하지 않는다.

## 재현

```bash
python3 -m unittest discover -s scripts/tests -p 'test_replication_*.py'
```

```bash
python3 -m unittest discover -s scripts/tests -p 'test_subscription_metrics.py'
```

```bash
infra/envs/target-workloads/testing/subscription_health_lab.py --out lab.json
```

```bash
infra/envs/target-workloads/testing/subscription_health_cnpg.py --out cnpg.json
```

`t8-lag-*`, `t8-health-k3s-*` 컨테이너만 만들고 지운다.
