# 관측 경로 알림 런북 (T37)

알림 규칙은 [`services/observability/rules/alerts.yml`](../../services/observability/rules/alerts.yml)이고, 규칙마다 `promtool test rules` 단위 시험이 있다.

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
3. 클러스터에서 플랫폼 지표 저장소 포트로 가는 Tailscale 경로가 살아 있는가. 온프렘은 바깥 방향 연결만 쓴다.
4. 플랫폼 지표 저장소 자체가 살아 있는가. 모든 환경이 동시에 알리면 저장소 쪽 문제다.
5. 노드 시계가 맞는가. 생존 신호 시각은 Collector 호스트 시계다.

**조치:** 원인을 고친다. Collector를 재시작해도 디스크 큐의 지표는 남는다. 인증서 문제면 같은 CA로 다시 발급하고, 이름은 지표 저장소 `web.yml`의 `client_allowed_sans`에 있어야 한다.

## BravoTelemetryExportFailing

**뜻:** 최근 5분 동안 Collector가 플랫폼으로 지표를 보내다 실패한 적이 있다(`otelcol_exporter_send_failed_metric_points_total` 증가).

**한계:** 이 지표도 같은 경로로 전달된다. 그래서 경로가 완전히 끊기면 이 알림이 아니라 BravoEnvUnobservable이 뜨고, 이 알림은 경로가 돌아온 뒤에 뜬다. 이 알림이 잡는 것은 일부 실패(간헐적 TLS 오류, 저장소 거부)다.

**확인:** Collector 로그의 실패 이유를 본다. 지표 저장소가 표본을 거부했다면(늦게 도착한 표본이 10분 범위를 넘음, 형식 오류) 저장소 로그에도 남는다.

## BravoTelemetryQueueFilling

**뜻:** Collector 전송 대기열이 1분 넘게 절반 이상 차 있다. 대기열 크기는 5000이고, 가득 차면 새 지표를 버린다.

**확인:** 플랫폼으로 가는 경로가 느리거나 끊겼는가(BravoEnvUnobservable이 함께 떴는가). 앱 지표가 갑자기 늘었는가(지표 종류가 폭증하지 않았는가).

**조치:** 경로를 복구한다. 지표 종류가 폭증했다면 허용 목록(`collector.yaml`의 `transform/allowlist`) 밖 속성이 새지 않았는지 본다.

## BravoTelemetryRefused

**뜻:** 최근 5분 동안 Collector가 앱 지표를 거부했다(`otelcol_receiver_refused_metric_points_total` 증가). 대부분 메모리 상한(`memory_limiter`) 때문이다.

**확인:** Collector 메모리 사용량과 Pod 자원 상한, 앱이 보내는 지표 양.

**조치:** 앱의 지표 양을 줄이거나 Collector 자원 상한을 조정한다. 메모리 상한을 끄지 않는다. 상한이 없으면 Collector가 죽어 생존 신호까지 끊긴다.
