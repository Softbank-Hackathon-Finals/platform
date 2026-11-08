# 관측 경로 알림 런북 (T37)

알림 규칙은 [`services/observability/deploy/platform/rules/alerts.yml`](../../services/observability/deploy/platform/rules/alerts.yml)이고, 규칙마다 `promtool test rules` 단위 시험이 있다.

모든 알림은 **관측 경로**의 문제다. SHIFT · WATCH 판정은 T18 창 요약만 쓰므로(ADR-0010) 이 알림으로 판정이 바뀌지는 않는다. 다만 콘솔의 환경별 서버 지표는 "확인 불가"가 된다.

공통으로 하지 말 것:
- 알림을 끄려고 규칙 기준값을 바꾸지 않는다. 바꿀 때는 PR로 바꾸고 단위 시험도 함께 고친다.
- 확인 불가 상태를 "정상"으로 표시하지 않는다.

## BravoEnvUnobservable

**뜻:** 한 환경의 Collector 생존 신호(`up{bravo_signal="collector-self"}`)가 30초 넘게 오지 않았거나, 5분 조회 범위 안에 아예 없다.

**영향:** 그 환경의 서버 지표를 볼 수 없다. Collector가 디스크 큐에 쌓아 둔 지표는 경로가 돌아오면 늦게 도착한다. 지표 저장소는 10분까지 늦은 표본을 받는다.

**확인 순서**
1. Collector Pod가 떠 있는가: `kubectl -n <관측 네임스페이스> get pods`. 상태 확인 엔드포인트는 13133이다.
2. Collector 로그에 TLS 오류(인증서 만료, 이름이 허용 목록에 없음)나 전송 실패가 있는가.
3. 클러스터에서 플랫폼 지표 저장소(`bravo-metrics.<tailnet>.ts.net:9090`)로 가는 Tailscale 경로가 살아 있는가. 온프렘은 바깥 방향 연결만 쓴다. 플랫폼 쪽은 `tailscale-system`에서 `tailscale.com/parent-resource=bravo-prometheus-tailnet` proxy Pod가 Running인가.
4. 플랫폼 지표 저장소 자체가 살아 있는가. 모든 환경이 동시에 알리면 저장소 쪽 문제다.
5. 노드 시계가 맞는가. 생존 신호 시각은 Collector 호스트 시계다.

**조치:** 원인을 고친다. Collector를 재시작해도 디스크 큐의 지표는 남는다. 인증서 문제면 같은 CA로 다시 발급하고, 이름은 지표 저장소 `deploy/platform/web.yml`의 `client_allowed_sans`에 있어야 한다.

## BravoTelemetryExportFailing

**뜻:** 최근 5분 동안 Collector가 플랫폼으로 지표를 보내다 실패한 적이 있다(`otelcol_exporter_send_failed_metric_points_total` 증가).

**한계:** 이 지표도 같은 경로로 전달된다. 그래서 경로가 완전히 끊기면 이 알림이 아니라 BravoEnvUnobservable이 뜨고, 이 알림은 경로가 돌아온 뒤에 뜬다. 이 알림이 잡는 것은 일부 실패(간헐적 TLS 오류, 저장소 거부)다.

**확인:** Collector 로그의 실패 이유를 본다. 지표 저장소가 표본을 거부했다면(늦게 도착한 표본이 10분 범위를 넘음, 형식 오류) 저장소 로그에도 남는다.

## BravoTelemetryQueueFilling

**뜻:** Collector 전송 대기열이 1분 넘게 절반 이상 차 있다. 대기열 크기는 5000이고, 가득 차면 새 지표를 버린다.

**확인:** 플랫폼으로 가는 경로가 느리거나 끊겼는가(BravoEnvUnobservable이 함께 떴는가). 앱 지표가 갑자기 늘었는가(지표 종류가 폭증하지 않았는가).

**조치:** 경로를 복구한다. 지표 종류가 폭증했다면 허용 목록(`deploy/collector/collector.yaml`의 `transform/allowlist`) 밖 속성이 새지 않았는지 본다.

## BravoTelemetryRefused

**뜻:** 최근 5분 동안 Collector가 앱 지표를 거부했다(`otelcol_receiver_refused_metric_points_total` 증가). 대부분 메모리 상한(`memory_limiter`) 때문이다.

**확인:** Collector 메모리 사용량과 Pod 자원 상한, 앱이 보내는 지표 양.

**조치:** 앱의 지표 양을 줄이거나 Collector 자원 상한을 조정한다. 메모리 상한을 끄지 않는다. 상한이 없으면 Collector가 죽어 생존 신호까지 끊긴다.

## BravoDatabaseDown

**뜻:** CNPG 인스턴스의 지표 수집기가 PostgreSQL에 접속하지 못하거나(`cnpg_collector_up == 0`), Collector가 그 Pod의 지표를 긁지 못한 상태(`up == 0`)가 1분 넘게 이어진다. 둘 다 DB 긁기 경로의 출처 라벨(`bravo_signal="cnpg-scrape"`)이 있는 지표만 본다. 앱이 같은 이름으로 보낸 지표는 이 알림을 만들지 않는다.

**영향:** 그 DB의 쓰기 · 읽기가 실패하고 있을 수 있다. 이 알림은 판정 입력이 아니다. 컷오버와 되돌리기는 Executor가 DB에서 직접 확인한다.

**확인 순서**
1. `kubectl -n <DB 네임스페이스> get cluster,pods`
2. `kubectl cnpg status <클러스터>`(CNPG 플러그인이 있으면): 인스턴스 상태, 기본 · 대기 역할
3. 컷오버 · 되돌리기 중이면 상태 머신이 SAFE_HOLD인지 확인한다.

**하지 말 것:** 이 알림만 보고 쓰기를 다른 DB로 열지 않는다. 쓰기를 받는 DB는 항상 최대 하나다(설계 원칙).

## BravoReplicationSlotLagHigh

**뜻:** 논리 복제 슬롯이 64MiB 넘게 밀린 상태가 2분 넘게 이어진다(`cnpg_pg_replication_slots_pg_wal_lsn_diff{slot_type="logical"}`). 64MiB는 설계 5.3 데이터 크기(200MB 이하)의 약 3분의 1이고, 실측 뒤 조정하는 제안값이다.

**영향:** 이만큼 밀린 채로 컷오버하면 따라잡는 시간이 쓰기 정지 예산(5초)을 넘긴다. Executor는 컷오버 1단계에서 복제 지연을 직접 확인하고, 기준을 넘으면 시작하지 않는다.

**확인:** 구독 쪽(목적지) 적용이 멈췄는가(`pg_stat_subscription`), Tailscale 경로가 느리거나 DERP를 거치는가(T8 실측), 출발지 쓰기가 갑자기 늘었는가.

## BravoReplicationSlotInactive

**뜻:** 논리 복제 슬롯이 10분 넘게 비활성이다(`cnpg_pg_replication_slots_active == 0`). 구독자가 붙어 있지 않다.

**영향:** 비활성 슬롯은 WAL을 계속 붙잡아 디스크를 채운다. 이전 작업 중이면 복제가 멈춘 것이다.

**확인 · 조치:** 구독이 살아 있는가(목적지 `pg_subscription`). 이전이 끝났거나 취소된 슬롯이면 담당(T8)이 정리한다. 이 알림을 보고 슬롯을 지우지 않는다. 되돌리기 경로(역방향 복제)일 수 있다.

구독이 켜진 DB(지금 받는 쪽)의 슬롯은 컷오버 전 되돌리기 대기 슬롯이라 이 알림과 지연 알림에서 빠진다. 대기 슬롯이 붙잡은 WAL은 [BravoStandbySlotWalHigh](#bravostandbyslotwalhigh)가 본다.

## BravoStandbySlotWalHigh

**뜻:** 구독을 받고 있는 DB의 논리 슬롯(컷오버 뒤 켜질 역방향 `t8_target_to_source` 등)이 WAL을 1GiB 넘게 붙잡고 있다.

**영향:** 대기 자체는 설계대로다(`docs/operations/t8-replication-health.md`). 다만 붙잡은 WAL이 계속 늘면 목적지 DB 볼륨을 채운다.

**확인 · 조치:** `pg_replication_slots`의 `restart_lsn`과 볼륨 여유를 본다. 슬롯은 되돌리기 경로라 이 알림만으로 지우거나 전진시키지 않는다. 담당(T8)이 컷오버 일정과 함께 정리 · 재생성 여부를 정한다.

## BravoWalArchivingFailing

**뜻:** 최근 10분 사이 WAL 보관이 실패했다(`cnpg_pg_stat_archiver_failed_count_total` 증가).

**영향:** 백업 · 복원(T35, DR-01)의 복구 지점이 끊긴다. 보관이 계속 실패하면 WAL이 쌓여 디스크를 채운다.

**확인:** CNPG 클러스터 상태와 Pod 로그의 보관 오류(저장소 자격 · 권한 · 네트워크). 출발지는 S3, 목적지는 SeaweedFS다.

## BravoPgBouncerDown

**뜻:** exporter가 PgBouncer 관리 DB에 접속하지 못하거나(`pgbouncer_up == 0`), Collector가 exporter를 긁지 못한 상태(`up == 0`)가 1분 넘게 이어진다. 둘 다 PgBouncer 긁기 경로의 출처 라벨(`bravo_signal="pgbouncer-scrape"`)이 있는 지표만 본다.

**영향:** 앱 → PgBouncer → DB 경로가 끊겼을 수 있다. exporter만의 문제(통계 계정 · TLS · NetworkPolicy)일 수도 있다. 이 알림은 판정 입력이 아니다. 컷오버는 Executor가 PgBouncer에서 직접 확인한다.

**확인 순서**
1. `kubectl -n <pooler 네임스페이스> get deploy,pods -l app=<pooler>`: PgBouncer Pod가 Ready인가
2. 앱의 `/api/health/db`가 성공하는가. 성공하면 exporter 쪽 문제다
3. `kubectl -n bravo-observability logs deploy/bravo-pgbouncer-exporter`: 인증 실패(통계 계정 · auth_file), TLS 검증 실패(CA · 서버 이름), 연결 시간 초과(NetworkPolicy)

**하지 말 것:** 통계 계정 대신 컷오버 제어 계정(`admin_users`)을 exporter에 넣지 않는다. 그 계정은 PAUSE · RELOAD를 할 수 있다.

## BravoPgBouncerPausedTooLong

**뜻:** PgBouncer DB 항목이 PAUSE 상태로 2분 넘게 있다. 컷오버의 PAUSE는 보통 몇 초다.

**영향:** 그 DB로 가는 앱 요청이 오류 없이 대기한다. 사용자는 응답 없음으로 느낀다.

**확인 순서**
1. 상태 머신이 컷오버 중이거나 SAFE_HOLD인가. SAFE_HOLD면 의도한 정지일 수 있다
2. Executor 기록에서 PAUSE 뒤 RESUME이 실패했거나 실행되지 않았는가
3. `SHOW DATABASES`로 `paused` 값을 직접 확인한다(통계 계정으로 볼 수 있다)

**하지 말 것:** 상태 머신과 Executor를 확인하지 않고 RESUME하지 않는다. 쓰기를 받는 DB가 둘이 될 수 있다(설계 원칙).

## BravoPgBouncerClientsWaiting

**뜻:** 같은 환경·pooler·DB에서 `paused=0`을 관측했고, 서버 연결을 기다리는 클라이언트가 2분 넘게 있다.

PAUSE 지표가 없으면 이 경보는 발화하지 않는다. 조회 API는 대기 수를 보존하되 `paused=null`·`UNOBSERVABLE`로 표시한다. 경보가 없다는 사실만으로 정상 또는 PAUSE 해제를 판단하지 않는다.

**영향:** 풀이 모자라거나(`default_pool_size`) DB가 느려 앱 요청이 밀린다.

**확인 순서**
1. 조회 API PgBouncer 뷰에서 그 DB의 활성 · 대기 클라이언트와 활성 서버 연결 수
2. DB 쪽: `BravoDatabaseDown`, 느린 쿼리, 잠금
3. 부하가 데모 예상보다 큰가(T18 실행 요약)

**하지 말 것:** 원인을 보지 않고 풀 크기만 늘리지 않는다. DB 연결 한도를 넘길 수 있다.


## BravoForwardSubscriptionOff

정방향 구독이 2분 넘게 OFF다. 현재 컷오버·이관 작업창과 단일 writer 상태를 먼저 확인한다. 계획된 정지이면 해당 실행의 증빙과 연결하고, 예상하지 않은 정지이면 `infra/envs/target-workloads/replication_incident.py`로 오류 종류를 읽는다. 경보만으로 `ENABLE`하지 않는다. 역방향 OFF는 정상 준비 상태여서 이 경보에서 제외한다.

## BravoForwardSubscriptionError

최근 10분 안에 적용 오류가 증가했다. `disable_on_error=false`에서는 구독이 켜진 채 오류를 반복할 수 있으므로 enabled와 worker만으로 정상이라고 하지 않는다. 오류 로그의 SQLSTATE와 출발지 슬롯 active·WAL 지연을 함께 확인한다. 연결 오류와 행 충돌을 구분하고 행 값·접속 문자열은 공유하지 않는다. 누적 오류 수 자체가 0보다 크다는 이유로 경보하지 않는다.

## BravoForwardSubscriptionRetrying

5분 안에 적용 오류가 5건 이상 늘었다. 임계값은 T8 격리 시험의 충돌 30초당 7건을 근거로 한 초기 운영값이다. 실제 오탐률을 측정한 값은 아니다. 원인·WAL 누적량을 확인하고 기존 복제 운영 담당이 복구한다. 오류를 숨기려고 통계를 reset하거나 충돌 데이터를 지우지 않는다.

## BravoForwardReplicationNotRecovering

출발지의 정방향 논리 슬롯이 2분 넘게 비활성이다. 목적지 구독 상태, 연결 경로, WAL 보존량을 같이 확인한다. worker가 존재해도 연결 중일 수 있다. 컷오버 뒤 계획된 정방향 정지와 장애를 구분한다. 역방향 슬롯은 포함하지 않는다. 기존 `BravoReplicationSlotLagHigh`가 WAL 64MiB 초과를 별도로 감시한다.

## BravoForwardSubscriptionUnobservable

정방향 enabled 또는 적용 오류 지표가 없거나 75초보다 오래됐고 이 상태가 1분 이어졌다. 관측 경로의 문제이며 복제가 정상이라는 뜻이 아니다. 목적지 Cluster의 customQueriesConfigMap, exporter의 쿼리 오류, Collector의 DB namespace·RBAC·9187 접근, 플랫폼 OTLP 전송을 순서대로 확인한다. 기본 CNPG 쿼리 참조를 제거하지 않고 `target-subscription-metrics`를 더한다. CNPG 캐시 30초와 scrape 15초를 고려해 복구 뒤 새 표본 시각을 확인한다. 앱이 같은 지표 이름을 보내도 신뢰 라벨 없는 시계열은 경보를 해소하지 않는다.


## BravoForwardSlotUnobservable

출발지 Collector가 살아 있어도 DB 수집 설정이 없으면 슬롯 오류·WAL 경보는 작동하지 않는다. 출발지 슬롯 표본 부재·75초 초과가 1분 이어지면 확인 불가로 알린다. `collector-db` 설정, `source-dev` Pod 발견 RBAC, 9187 네트워크 접근, 실제 CNPG query 오류와 슬롯 존재를 확인한다. 새 슬롯을 임의로 만들거나 지표 부재를 슬롯 비활성 0으로 바꾸지 않는다. 정상 표본 수신 시 경보가 해소되는지 확인한다.

## 역할 기준 경보 (ADR-0034)

아래 경보는 위 환경 이름 기준 경보를 그대로 둔 채 더한 것이다. 출발지 · 목적지를 Collector의 `BRAVO_ROLE`이 붙인 `bravo_role`(`source` · `target`)로 고르므로 환경 종류(aws · gcp · onprem)와 무관하다. 같은 사건을 두 번 알리지 않도록 P0 이름이 맡는 쌍(`aws`=source, `onprem`=target)은 환경 이름 경보에 맡긴다. 그래서 aws → onprem 배포에서는 한 역할의 Collector가 5분 조회 범위를 넘어 통째로 사라진 경우(BravoRoleUnobservable)만 함께 뜬다. 역할 라벨을 붙이는 Collector가 하나도 없으면(역할 도입 전 배포) 역할 경보는 조용하다.

## BravoRoleUnobservable

한 역할 환경의 Collector 생존 신호가 30초 넘게 오지 않았거나, 그 역할을 보고하는 환경이 하나도 없다. 확인 순서와 조치는 [BravoEnvUnobservable](#bravoenvunobservable)과 같다. 역할 자체가 없다고 알리면 Collector 설정(`bravo-collector-env`)의 `BRAVO_ROLE`이 빠졌거나 `source` · `target` 밖의 값인지 먼저 확인한다(빈 값이면 `aws`=source, `onprem`=target 외의 환경은 역할 라벨이 지워진다).

## BravoForwardSubscriptionOffByRole

P0 이름이 아닌 목적지 역할 환경의 정방향 구독이 2분 넘게 OFF다. 절차는 [BravoForwardSubscriptionOff](#bravoforwardsubscriptionoff)와 같다.

## BravoForwardSubscriptionErrorByRole

P0 이름이 아닌 목적지 역할 환경에서 최근 10분 안에 적용 오류가 증가했다. 절차는 [BravoForwardSubscriptionError](#bravoforwardsubscriptionerror)와 같다.

## BravoForwardSubscriptionRetryingByRole

P0 이름이 아닌 목적지 역할 환경에서 5분 안에 적용 오류가 5건 이상 늘었다. 절차는 [BravoForwardSubscriptionRetrying](#bravoforwardsubscriptionretrying)과 같다.

## BravoForwardReplicationNotRecoveringByRole

P0 이름이 아닌 출발지 역할 환경의 정방향 논리 슬롯이 2분 넘게 비활성이다. 절차는 [BravoForwardReplicationNotRecovering](#bravoforwardreplicationnotrecovering)과 같다.

## BravoForwardSubscriptionUnobservableByRole

P0 이름이 아닌 목적지 역할 환경의 정방향 enabled 또는 적용 오류 지표가 없거나 75초보다 오래됐다. 지표 부재는 그 역할의 Collector가 살아 있을 때만 판정한다(Collector가 사라지면 BravoRoleUnobservable이 알린다). 절차는 [BravoForwardSubscriptionUnobservable](#bravoforwardsubscriptionunobservable)과 같다.

## BravoForwardSlotUnobservableByRole

P0 이름이 아닌 출발지 역할 환경의 정방향 슬롯 지표가 없거나 75초보다 오래됐다. 절차는 [BravoForwardSlotUnobservable](#bravoforwardslotunobservable)과 같다.
