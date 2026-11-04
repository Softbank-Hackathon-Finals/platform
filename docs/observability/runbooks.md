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

## BravoWalArchivingFailing

**뜻:** 최근 10분 사이 WAL 보관이 실패했다(`cnpg_pg_stat_archiver_failed_count_total` 증가).

**영향:** 백업 · 복원(T35, DR-01)의 복구 지점이 끊긴다. 보관이 계속 실패하면 WAL이 쌓여 디스크를 채운다.

**확인:** CNPG 클러스터 상태와 Pod 로그의 보관 오류(저장소 자격 · 권한 · 네트워크). 출발지는 S3, 목적지는 SeaweedFS다.
