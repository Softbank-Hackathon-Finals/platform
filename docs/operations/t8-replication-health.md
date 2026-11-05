# T8 정방향 복제 상태: 꺼짐 원인 확인 · 시간 초과 · 설정 분리 · 지표

**구독이 꺼지면 아래 한 줄로 꺼진 시각과 오류 종류를 값 노출 없이 읽을 수 있다.** 복제 시간 초과는 양쪽 모두 30초로 렌더한다([11/6 새벽 끊김](#116-새벽-끊김-원인과-대응)). 정방향 `disable_on_error`는 역방향과 따로 렌더하며, 기본값은 정방향 `false` · 역방향 `true`다. **실제 정방향 객체는 11/6 04:16 KST에 `true`로 되돌려졌고, 누가 바꿨는지 확인하고 있다**([지금 상태](#지금-상태-116-0425-kst)). 목적지 구독 상태와 적용 오류 수는 CNPG 지표로 나온다. 실제 클러스터에는 이 문서의 어떤 변경도 적용하지 않았다.

실제 로그와 설정은 읽기만 했다. 그 밖의 숫자는 임시 환경에서 잰 값이다([재지 못한 것](#재지-못한-것)). 원값은 [11/6 복제 상태 측정](../evidence/t8-replication-health-2026-11-06/README.md)과 [11/6 시간 초과 측정](../evidence/t8-replication-timeout-2026-11-06/README.md)에 있다.

## 11/6 새벽 끊김: 원인과 대응

**정방향 연결이 5초 시간 초과로 두 번 끊겼다. 중계 경로가 몇 초 막히면 5초로는 버티지 못하므로, 양쪽 시간 초과를 30초로 올린다.**

두 DB는 연결이 살아 있는지 서로 확인한다. 보내는 쪽은 받는 쪽의 답이 정해진 시간 동안 없으면 끊고, 받는 쪽은 정해진 시간 동안 아무것도 받지 못하면 끊는다. 그 시간이 5초였다.

### 확인한 것 (실제 로그와 설정, 읽기만)

| 항목 | 값 |
| --- | --- |
| 시각 | 11/6 03:45:36, 03:49:41 KST (18:45:36Z, 18:49:41Z) |
| 목적지 | 수신 시간 초과로 적용 워커 종료(`terminating logical replication worker due to timeout`, 08006) 2회 |
| 출발지 | 같은 시각 송신 시간 초과로 walsender 종료(`terminating walsender process due to replication timeout`) 2회. 두 쪽 기록은 0.03초 안에 있다(두 기계의 시계 차이는 모른다) |
| 끊기기 전 연결 | 02:59 재개 뒤 46분 12초, 그다음 3분 53초 |
| 그 뒤 | 정방향 `disable_on_error=false`라 꺼지지 않았다. 두 번 모두 새 워커가 0.06초 안에 떴다. 첫 번째 뒤에는 약 11.5초 뒤 출발지에 다시 붙었다(두 번째 연결의 길이로 계산). 왜 11.5초가 걸렸는지는 모른다 |
| 설정 | 양쪽 `wal_sender_timeout` · `wal_receiver_timeout` 5초(설정 파일, CNPG 기본값), `wal_receiver_status_interval` 10초, TCP keepalive · `tcp_user_timeout` 0(운영체제 기본). PostgreSQL 18.4 |
| 경로 | tailnet 세 경로 모두 DERP(중계) `tok`, 왕복 67~107ms, MTU 1280 통과(03:51 양방향 측정, 총괄 세션) |

원값은 [incident.json](../evidence/t8-replication-timeout-2026-11-06/incident.json)이다. 시각 · 고정 라벨 · 설정값만 남겼다.

### 지금 상태 (11/6 04:25 KST)

**정방향 `disable_on_error`가 다시 `true`라, 다음 5초 시간 초과 한 번에 정방향이 스스로 꺼질 수 있다.**

- 04:16:10 KST(19:16:10Z)에 목적지 `t8-source-to-target` Subscription의 `disable_on_error`가 `kubectl patch`로 `true`가 됐다. 객체의 managedFields에 `kubectl-patch`가 그 시각에 이 필드를 바꾼 기록이 있고, CNPG는 2초 뒤 반영했다. 누가 바꿨는지는 확인하고 있다.
- 04:25 읽기(총괄 세션): 구독 켜짐, `disable_on_error=true`, 적용 오류 5, 워커 있음. 행 수는 양쪽이 같고 슬롯은 활성이다. 18:49:41Z 뒤로 시간 초과는 없다.
- 이 변경은 `disable_on_error`를 바꾸지 않는다. 렌더 기본값 `false`와 실제 값 중 어느 쪽으로 맞출지는 바꾼 사람과 정한다. 시간 초과 30초를 먼저 적용하면 위험이 준다. lab에서 30초는 `true`여도 4초 정체에 한 번도 꺼지지 않았다.

**양쪽이 동시에 끊었고 직후 경로가 정상이었으므로, 경로가 끊긴 것이 아니라 몇 초 막힌 것으로 본다.** 패킷을 잡아 본 것은 아니어서 추정이다. 무엇이 막았는지(중계 서버, 노드 네트워크)는 모른다.

### 추정: 11/5 23:50 무렵 꺼진 것도 같은 원인

**같은 시간 초과에 정방향 `disable_on_error=true`가 겹쳐, 한 번 끊기자 구독이 꺼지고 다시 켜지지 않은 것으로 본다.** 그 시각의 목적지 로그는 남아 있지 않다(11/6 01:48 KST부터). 출발지 로그는 11/5 23:46:46 KST부터 있는데, 02:59 재개 전까지 walsender 기록이 하나도 없다. 그러니 정방향 연결은 23:46 이전에 이미 끊겨 있었고, 그 끊김의 원인과 정확한 시각은 남아 있지 않다. 그때 `true`였다는 것은 총괄 세션의 기록이다.

### 11/4의 "5초 유지"와 판단이 다른 이유

11/4에는 패킷을 모두 버리는 실제 단절(3~40초)을 쟀고, 정방향 `disable_on_error`가 켜져 있었다. 그 조건에서는 시간 초과를 올려도 단절이 길면 결국 꺼졌다. 이번 실패는 단절이 아니라 짧은 정체이고, 정방향은 이제 꺼지지 않는다. 그래서 아래 lab에서 짧은 정체를 따로 쟀다.

### lab: 짧은 정체에서 5초와 30초

**30초는 정체 15회에서 연결이 한 번도 끊기지 않았다. 5초는 지연 4초부터 끊겼고, `disable_on_error=true`와 겹치면 구독이 꺼졌다.** 대가로 실제 단절 20초의 회복은 5초보다 약 5.4초 늦었다.

임시 PostgreSQL 18.4 두 대, 가정한 netem 100Mbps · RTT 90ms(잰 값 110.9ms), 쓰기 50건/초, 조건마다 3회. 정체는 10초 동안 양방향에 한 방향 지연을 더한 것이고 패킷은 버리지 않았다. 단절은 패킷을 모두 버렸다. 구독은 렌더한 값(정방향 `false`)으로 만들었고, `disable_on_error=true` 행만 바꿨다. "연결 끊김"은 적용 오류 증가 · 워커나 walsender 교체 · 시간 초과 로그 중 하나가 있었던 횟수다.

| 조건 | 시간 초과 | 횟수 | 연결 끊김 | 구독 꺼짐 | 적용 오류 증가 | 끝난 뒤 행 도착(중앙값 / 최대, 초) | 잃은 행 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 정체 +2초, 쓰기 중 | 5초 | 3 | 0 | 0 | 0 | 2.33 / 2.37 | 0 |
| 정체 +2초, 쓰기 중 | **30초** | 3 | 0 | 0 | 0 | 2.33 / 2.34 | 0 |
| 정체 +4초, 쓰기 중 | 5초 | 3 | 2 | 0 | 0~1 | 4.13 / 4.42 | 0 |
| 정체 +4초, 쓰기 중 | **30초** | 3 | 0 | 0 | 0 | 3.63 / 3.88 | 0 |
| 정체 +6초, 쓰기 중 | 5초 | 3 | 3 | 0 | 1 | 6.47 / 6.71 | 0 |
| 정체 +6초, 쓰기 중 | **30초** | 3 | 0 | 0 | 0 | 5.97 / 6.51 | 0 |
| 정체 +4초, 쓰기 없음 | 5초 | 3 | 3 | 0 | 1 | 4.16 / 4.18 | 0 |
| 정체 +4초, 쓰기 없음 | **30초** | 3 | 0 | 0 | 0 | 3.91 / 4.16 | 0 |
| 정체 +4초, 쓰기 중, `disable_on_error=true` | 5초 | 3 | 2 | 2 | 0~1 | 3.63 / 3.63 (꺼진 2회는 재지 않음) | 0 |
| 정체 +4초, 쓰기 중, `disable_on_error=true` | **30초** | 3 | 0 | 0 | 0 | 3.63 / 3.63 | 0 |
| 단절 20초, 쓰기 중 | 5초 | 3 | 3 | 0 | 1 | 6.48 / 6.49 | 0 |
| 단절 20초, 쓰기 중 | **30초** | 3 | 3 | 0 | 1 | 11.89 / 11.9 | 0 |

- **쓰기가 없을 때 더 잘 끊겼다.** 5초에서 4초 정체가 쓰기 중에는 3회 중 2회, 쓰기가 없을 때는 3회 모두 끊겼다. 쓰기가 없으면 확인 신호가 몇 초에 한 번만 오가, 정체가 그 신호를 바로 늦추기 때문으로 본다(추정). 실제 끊김(03:45)은 쓰기가 적은 새벽이었다.
- **5초에 `true`면 4초 정체로 구독이 꺼졌다(3회 중 2회).** 11/5 추정과 같은 모양이다. 30초에서는 같은 조건에서 한 번도 꺼지지 않았다.
- **20초 단절에는 두 값 모두 연결이 끊겼다가 스스로 다시 붙었다.** 30초도 단절 뒤 TCP 재전송 간격이 벌어져 30초를 넘긴 것으로 본다(추정). 다시 흐르기까지 중앙값이 5초 6.48초, 30초 11.89초였다.
- **정체 뒤 행 도착 시간은 정체 길이와 비슷하다.** 정체 동안 늦춰진 패킷이 그만큼 늦게 도착하기 때문이고, 연결이 끊긴 것과는 관계없다.
- 모든 시행에서 잃은 행은 0이고, 역방향은 끝까지 OFF · `disable_on_error=true`였다. 시간 초과는 워커를 띄운 채 reload로 6번 바꿨고, 매번 적용 워커와 walsender의 PID가 그대로였으며 `pending_restart`는 0이었다.

### 대응

| 무엇 | 어디 | 상태 |
| --- | --- | --- |
| 양쪽 Cluster 렌더에 `wal_sender_timeout` · `wal_receiver_timeout` 30초. 정방향(출발지 송신 · 목적지 수신)과 역방향(목적지 송신 · 출발지 수신) 네 값이 모두 30초다 | `target-workloads/render.py`, `source-workloads/render.py`의 `REPLICATION_TIMEOUTS`. 두 렌더가 같은지, 방향별 네 값이 30초인지 시험이 고정한다 | 이 변경 |
| 정방향 `disable_on_error` 렌더 기본값 `false`, 역방향 `true` | `replication.py`. 렌더가 `true`면 렌더로 객체를 다시 적용할 때 정방향이 다시 켜진다. 실제 객체는 04:16에 `true`로 되돌려졌다(위 지금 상태) | 이 변경. 실제 값은 바꾼 사람과 정한다 |
| 떠 있는 두 클러스터에 30초 적용 | 아래 절차 | 승인 대기. 실행하지 않았다 |

**역방향도 같은 경로를 쓴다.** 지금은 꺼져 있고(출발지 `t8_target_to_source` OFF, `disable_on_error=true`, 목적지 슬롯 비활성 `reserved`, 03:56 총괄 세션 확인) 컷오버 뒤에 켜진다. 5초 그대로면 같은 정체에 끊기고, `true`라서 스스로 꺼진다. 그러면 되돌리기 전 복제 경로 확인(ADR-0016)과 Executor의 원상 복구 확인(`runner.py`의 구독 상태 대조), T27 차단 시험의 기준선(`BASELINE_NOT_STREAMING`)이 막힌다. 그래서 시간 초과는 역방향 두 끝에도 같이 올린다.

**역방향 `disable_on_error`는 `true`로 둔다. 시간 초과를 올려도 남는 위험은 이렇다.**

- **30초를 넘는 정체나 실제 단절에는 여전히 꺼진다.** lab에서 30초는 20초 단절에도 연결이 끊겼다. 단절 뒤 TCP 재전송 간격이 벌어져 30초를 넘긴 것으로 본다(추정). 정방향은 다시 붙지만 역방향은 꺼진 채 남는다.
- **꺼진 역방향은 사람이 켠다.** `resume_replication.py --direction reverse`가 정방향과 같은 규칙으로 확인하고 켠다([컷오버 뒤 역방향 재개](#컷오버-뒤-역방향-재개)). 출발지에 행이 더 많으면 켜지 않는다.
- **역방향은 지표가 없다.** 출발지(T6)에 구독 지표를 넣지 않았고 T37 알람도 정방향만 제안했다. 컷오버 뒤 역방향이 꺼지면 되돌리기 직전 경로 확인에서야 알 수 있다.
- **그래도 `true`인 이유.** 컷오버 뒤 역방향의 오류는 대개 출발지에 들어오면 안 되는 쓰기와의 충돌이다. 끝없이 재시도하면 그 쓰기를 숨기고, 멈춰야 사람이 본다.

**30초로 잃는 것.** 경로가 정말로 끊기면 알아채는 데 최대 30초 걸린다. 끊긴 walsender가 그동안 슬롯을 쥐고 있으면 재개 명령은 `slot_in_use`로 멈춘다. 그때는 30초 뒤 다시 돌린다.

**Executor는 바꿀 것이 없다.** `disable_on_error`를 읽지 않는다. 사전 점검에서 구독과 워커를 보고, 지연 한도(1MiB)와 복제 경로 왕복 시험(ADR-0016, 10초)으로 멈춘다. 충돌로 재시도만 하는 구독은 켜져 있어도 barrier가 도착하지 않으니 왕복 시험에서 멈춘다. 코드를 읽고 내린 판단이고, 이 조합으로 Executor를 돌려 보지는 않았다(T12).

### 떠 있는 클러스터에 적용

**두 설정 모두 재시작 없이 reload로 바뀐다**(`pg_settings.context`가 `wal_sender_timeout`은 `user`, `wal_receiver_timeout`은 `sighup`). 한 Cluster의 명령 하나가 두 값을 함께 바꾸므로, 두 명령으로 양방향 네 값이 모두 바뀐다. 출발지를 먼저, 목적지를 나중에 바꾼다. 한쪽만 30초인 동안은 다른 쪽 5초가 지금처럼 끊으므로 지금보다 나빠지지 않는다. 실행 중인 컷오버가 없을 때 한다.

```bash
kubectl -n source-dev patch cluster source-db --type merge -p '{"spec":{"postgresql":{"parameters":{"wal_sender_timeout":"30s","wal_receiver_timeout":"30s"}}}}'
```

```bash
kubectl -n target-dev patch cluster target-db --type merge -p '{"spec":{"postgresql":{"parameters":{"wal_sender_timeout":"30s","wal_receiver_timeout":"30s"}}}}'
```

확인은 쪽마다 한다. 두 값이 `30s`, `pending_restart`가 `f`, Pod의 UID와 재시작 횟수가 그대로여야 한다. 적용 워커와 walsender의 PID도 바뀌지 않아야 한다(lab에서 reload 6회 모두 유지).

```bash
kubectl -n target-dev exec target-db-1 -c postgres -- psql -X -q -At -d chat -c "BEGIN READ ONLY; SELECT name, setting, pending_restart FROM pg_settings WHERE name IN ('wal_sender_timeout', 'wal_receiver_timeout'); SELECT pid FROM pg_stat_subscription WHERE subname = 't8_source_to_target' AND worker_type = 'apply'; ROLLBACK;"
```

되돌리기는 두 명령의 `30s`를 `5s`로 바꿔 같은 순서로 실행한다. 이 순서와 확인, 되돌리기를 묶은 스크립트는 저장소 밖(총괄 세션 공유 노트 `t8-timeout-apply.sh`)에 있다.

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

## 컷오버 뒤 역방향 재개

**컷오버 뒤 역방향(`t8_target_to_source`)이 꺼지면 `resume_replication.py --direction reverse`로 다시 켠다.** 정방향 재개와 같은 명령이고 같은 판정 규칙을 쓴다. 두 DB의 역할만 바뀐다. 컷오버 뒤에는 목적지가 쓰기를 받아 보내고, 출발지가 받는다. 되돌리기 전 복제 경로 확인(ADR-0016)과 T27 차단 시험의 기준선(`BASELINE_NOT_STREAMING`)이 역방향이 흐르기를 전제하므로, 그 전에 쓴다.

실제 환경에서는 돌리지 않았다. 아래 숫자는 임시 컨테이너에서 잰 값이다([측정 기록](../evidence/t8-reverse-resume-2026-11-06/README.md)).

### 켜기 전에 확인하는 것

하나라도 어긋나면 아무것도 바꾸지 않고 `refusals`에 이유를 적는다. **어느 순간에도 쓰기를 받는 DB는 목적지 하나여야 한다.** 켠 뒤 따라잡는 동안에도 같은 조건을 계속 보고, 깨지면 시험 행을 넣기 전에 멈춘다.

| 확인 | 어긋날 때의 이유 |
| --- | --- |
| 목적지 앱 역할이 LOGIN이다(쓰기 DB) | `target_app_cannot_login` |
| 출발지 앱 역할이 NOLOGIN이고 세션이 없다 | `source_app_can_login`, `source_app_sessions` |
| 정방향 구독이 꺼져 있고 워커가 없다. 출발지의 정방향 슬롯도 쉬고 있다 | `forward_active`, `forward_slot_in_use` |
| 목적지의 역방향 슬롯이 있고, 논리 슬롯이며, WAL을 잃지 않았다(`reserved` · `extended`) | `slot_missing`, `slot_not_logical`, `slot_invalidated`, `slot_at_risk` |
| 역방향이 꺼진 채 슬롯을 쥔 다른 프로세스가 없다 | `slot_in_use` |
| 출발지에 목적지보다 많은 행이 없다(우회 쓰기) | `source_ahead_of_target` |
| 실행 중인 컷오버 · 제어 세션이 없다 | `executor_running`, `executor_state_unconfirmed`, `control_session_present` |
| 두 DB가 대조한 클러스터다 | `source_identity_mismatch`, `target_identity_mismatch`, `*_identity_not_pinned` |

**시험 행은 목적지에만 넣고 지운다.** 쓰기용 DSN은 `T8_RESUME_TARGET_PROBE_DSN` 하나뿐이다. 정방향의 `T8_RESUME_SOURCE_PROBE_DSN`이 있어도 쓰지 않는다. 그 DSN이 가리키는 DB가 검증한 목적지가 아니면 켜기 전에 멈춘다. 이 역할은 목적지 `cutover.barrier`에 INSERT · SELECT · DELETE 권한이 있어야 한다. 실제 목적지에 그런 역할이 있는지는 확인하지 않았다. 없으면 `--no-barrier-probe`로 돌리고, 출력에 그 선택이 남는다.

### 절차

변수 이름은 정방향과 같고, 각각 진짜 DB를 가리킨다. `T8_RESUME_SOURCE_DSN`은 출발지(구독 쪽, 켜는 권한이 필요하다: 구독 소유자이거나 `cutover.set_subscription` 실행), `T8_RESUME_TARGET_DSN`은 목적지(발행 쪽, `chat_replicator`면 된다)다.

1. 목적지 앱의 쓰기 차단 상태를 읽어 `--target-write-block on|off`로 넘긴다. 출력에 남기만 한다. 역방향에 `--source-write-block`을 주면 거부한다.
2. 옵션 없이 돌린다. `refusals`가 비어 있어야 한다. 출력에 `"direction": "reverse"`가 있다.

```bash
infra/envs/target-workloads/resume_replication.py --direction reverse --target-write-block off
```

3. `preflight.source.system_identifier`와 `preflight.target.system_identifier`를 의도한 클러스터와 사람이 대조한다. 출력의 이름은 진짜 DB를 말한다. 예를 들어 `preflight.target.target_slot_active`는 목적지에 있는 역방향 슬롯이다.
4. 대조한 값을 넘겨 `--apply`로 돌린다. 출발지에서 역방향을 한 번 켜고, 따라잡기 · 시험 행 왕복 · 유지 확인을 거친다.

```bash
infra/envs/target-workloads/resume_replication.py --direction reverse --apply --target-write-block off --source-system-id <값> --target-system-id <값>
```

시험 행 정리가 `pending`으로 끝나면 출력의 `marker.resume`을 그대로 돌린다. 그 안에 `--direction reverse`가 들어 있다.

**이 명령이 하지 않는 것.** 정방향을 다시 켜지 않는다(되돌리기는 Executor가 한다). 앱 역할 · 슬롯 · 쓰기 차단을 바꾸지 않는다. 출발지에는 어떤 쓰기도 하지 않는다. 옵션 없이 돌리면 아무것도 바꾸지 않는다.

### lab: 꺼짐 → 재개 → 따라잡기

**3회 모두 17개 확인을 통과했다. 역방향이 끊김으로 꺼진 뒤 명령이 다시 켰고, 확인된 쓰기 중 잃은 행은 없었으며, 쓰기 DB는 내내 목적지 하나였다.**

임시 PostgreSQL 18.4 두 대, 가정한 netem 100Mbps · RTT 30ms, 시간 초과 5초(빨리 끊기도록 11/6 이전 값), 목적지 쓰기 50건/초. 컷오버는 Executor 없이 lab이 같은 순서로 흉내 냈다: 출발지 NOLOGIN → 정방향 끔 → 목적지 시퀀스 맞춤 → 목적지 LOGIN → 역방향 켬.

| 단계 | 결과 (3회) |
| --- | --- |
| 컷오버 전에 역방향 명령 | 거부: `forward_active`, `forward_slot_in_use`, `source_app_can_login`, `target_app_cannot_login` |
| 컷오버 뒤에 정방향 명령 | 거부: `reverse_active`, `reverse_slot_in_use`, `source_app_cannot_login`, `target_app_can_login` |
| 링크를 끊음 | 5.72~5.76초 뒤 역방향이 스스로 꺼짐(수신 시간 초과 08006, 적용 오류 1, 슬롯 `reserved`) |
| 꺼진 동안 | 목적지에 약 8,380행이 더 쌓여 밀린 WAL이 약 17.1MiB |
| 옵션 없이 | 거부 없음, 아무것도 바꾸지 않음 |
| `--apply` | 출발지에서 역방향을 한 번 켬(제어 함수). 기준점 도착 2.06~3.08초, 시험 행 왕복 2.32~3.35초, 유지 확인 15초까지 전체 19~20초 |
| 끝난 뒤 | 확인된 쓰기 1,969~2,008건 중 출발지에 없는 행 0, 두 표 해시 같음, 시험 행 남음 0, 목적지만 LOGIN · 정방향 꺼짐 |
| 다시 돌림 | 다시 켜지 않고 확인만 함(`enable_submissions` 0) |

명령의 출력에는 lab 비밀번호 표시가 한 번도 나오지 않았다.

## 방향별 `disable_on_error`

구독이 있는 쪽을 `--side`로 고른다. `--side target`이 정방향, `--side source`가 역방향이다. 옵션을 주지 않으면 정방향은 `false`, 역방향은 `true`로 나온다. 역방향 출력은 이전과 바이트까지 같고, 정방향은 그 한 값만 다르다. 둘 다 시험이 고정한다.

```bash
python3 infra/envs/target-workloads/replication.py subscription --side target
```

정방향이 꺼져 있으면 끊김에서 스스로 회복하는 대신 "꺼짐" 신호가 사라지고, **충돌에는 끝없이 재시도한다.** 그래서 적용 오류 수 지표와 알람으로 본다. 알람은 아직 T37 담당과 합의 전이다([알람](#알람-t37-담당과-합의-후-적용)). 이전처럼 켜려면 `--disable-on-error true`를 준다.

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

- **실제 환경에 적용한 것은 없다.** AWS · VM · Tailscale 경로에서는 로그와 설정을 읽기만 했다. 30초의 실제 reload와 그 뒤의 동작은 적용 뒤 확인 절차로 본다.
- **11/5 23:50 사고의 원인.** 그 시각의 로그가 남아 있지 않아 추정만 했다.
- **실제 끊김과 정체의 모양.** netem 지연 · 손실과 walsender 정지는 대역이다. DERP 중계의 정체나 노드 재시작과 같다고 주장하지 않는다. 실제 정체가 몇 초였는지도 모른다.
- **실제 환경의 역방향 재개.** 컷오버가 끝난 실제 두 DB에서 `--direction reverse`를 돌린 적이 없다. 목적지에 시험 행용 역할이 있는지, 출발지에서 역방향을 켤 권한이 어느 역할에 있는지도 확인하지 않았다. Executor의 되돌리기와 이어서 돌려 보지도 않았다(T20 · T25).
- **역방향의 시간 초과.** lab은 정방향만 쟀다. 역방향은 같은 설정이 반대 방향에 걸린다는 PostgreSQL 동작에 기댄 판단이다.
- **30초에서의 실제 단절 회복과 TCP keepalive.** 단절 회복은 lab의 20초 단절만 쟀다. keepalive · `tcp_user_timeout`은 바꾸지도 재지도 않았다.
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
python3 -m unittest discover -s scripts/tests -p 'test_stall_lab.py'
```

```bash
infra/envs/target-workloads/testing/subscription_health_lab.py --out lab.json
```

```bash
infra/envs/target-workloads/testing/replication_timeout_lab.py --out timeout-lab.json
```

```bash
python3 -m unittest discover -s scripts/tests -p 'test_resume_*.py'
```

```bash
infra/envs/target-workloads/testing/replication_resume_lab.py reverse --out reverse-lab.json
```

```bash
infra/envs/target-workloads/testing/subscription_health_cnpg.py --out cnpg.json
```

`t8-lag-*`, `t8-health-k3s-*` 컨테이너만 만들고 지운다.
