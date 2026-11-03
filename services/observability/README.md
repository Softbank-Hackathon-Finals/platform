# 환경 횡단 지표 수집 (T37)

출발지 · 목적지 클러스터마다 OpenTelemetry Collector를 두고, 앱 지표를 플랫폼 지표 저장소(Prometheus)로 보낸다. 결정 배경은 [ADR-0010](../../docs/adr/0010-observability-boundary.md)이다. **판정 입력이 아니다.** SHIFT · WATCH 판정은 T18 창 요약만 쓴다([판정용 관측 계약](../../docs/observability/README.md)).

| 파일 | 내용 |
|---|---|
| [`collector.yaml`](collector.yaml) | 클러스터별 Collector 설정. 환경 이름(`BRAVO_ENV`)과 플랫폼 주소(`PLATFORM_OTLP_ENDPOINT`)는 배포가 정한다 |
| [`prometheus.yml`](prometheus.yml) | 플랫폼 지표 저장소 설정. OTLP로만 받는다 |
| [`testing/run.sh`](testing/run.sh) | 로컬 재현 시험 (Docker) |

## 동작

- **환경 이름은 Collector가 붙인다.** 앱이 보낸 `bravo.env`는 허용 목록에서 지워지고, Collector의 `BRAVO_ENV`로 덮어쓴다. 앱이 다른 환경을 사칭할 수 없다.
- **허용 목록:** 앱 지표는 아래 속성만 남는다. 채팅 본문, `Authorization`, 토큰, SQL 파라미터는 저장되지 않는다.
  - 리소스 속성: `service.name` · `service.instance.id`
  - 데이터 포인트 속성: `http.request.method` · `http.response.status_code` · `http.route`
- **수집 경로 생존 신호:** Collector가 자기 지표를 10초마다 긁어 같은 경로로 보낸다. 플랫폼은 환경별 최근 수신 시각을 아래 식으로 본다.

  ```promql
  time() - max by (bravo_env) (timestamp(up{job="bravo-collector"}))
  ```

  등록된 환경이 결과에 없거나 30초를 넘으면 그 환경은 "확인 불가"다. 보내는 방식이라 데이터가 오지 않는 것을 "정상"으로 읽지 않는다. 기준 30초는 수집 간격(10초)의 3배로 둔 제안값이다.
- **보존 기간 3일.** 데모와 증빙에 충분한 기간만 둔다. 판정의 근거는 판정 기록(`SliJudgement`)에 따로 남으므로 지표 저장소를 지워도 판정은 다시 계산할 수 있다.

## 보안 경계

- Prometheus의 OTLP 수신(`/api/v1/otlp`)과 조회 API에는 인증이 없다. 배포할 때 네트워크로 막는다.
  - 수신: 출발지 · 목적지 Collector 태그만 Tailscale ACL로 허용한다.
  - 조회: 플랫폼 안의 콘솔 API만 허용한다.
  - 이 경로를 열어 두면 누구나 다른 환경 이름으로 지표를 넣을 수 있다. 환경 덮어쓰기는 Collector를 거친 지표에만 적용된다.
- Collector의 OTLP 수신(4317 · 4318)은 클러스터 안에서만 받는다(Service는 ClusterIP, NetworkPolicy로 앱 네임스페이스만 허용).
- 리허설 네임스페이스에는 Collector로 가는 송신 예외를 열지 않는다(ADR-0002).
- 이미지는 태그와 digest로 고정한다: `otel/opentelemetry-collector-contrib:0.163.2`, `prom/prometheus:v3.15.1` (digest는 `testing/compose.yaml`).

## 검증

```sh
services/observability/testing/run.sh
```

Prometheus와 목적지 역할(`BRAVO_ENV=onprem`) Collector를 띄운다. 시험용 지표에는 `bravo.env="aws"` 위조값과 카나리 문자열을 넣은 `authorization` · `chat.body` 속성을 붙여 보낸다.

| 확인 | 결과 (2026-11-03 로컬) |
|---|---|
| 위조한 `bravo.env=aws`가 `onprem`으로 기록됨 | 통과 |
| 허용 목록의 `http.route`는 남음 | 통과 |
| 카나리 문자열이 모든 라벨 값에서 0건 (SEC-01.3과 같은 방식) | 통과 |
| 생존 신호: 최근 수신 1초 미만 | 통과 |
| Collector를 멈추면 30초 뒤 "확인 불가" | 통과 |

설정 검증도 통과했다: `otelcol-contrib validate`, `promtool check config`.

일부러 망가뜨린 설정으로도 돌려 봤다.
- 허용 목록을 빼면 카나리 검사가 실패한다.
- 허용 목록과 환경 덮어쓰기를 함께 빼면 위조 검사와 카나리 검사가 모두 실패한다.

시험이 이 보호 장치들을 실제로 확인한다는 뜻이다.

## 남은 일

이 PR은 설정과 로컬 시험까지다. 클러스터 배포와 실제 환경 검증은 아직 하지 않았다.

- **배포:** 플랫폼 RKE2의 지표 저장소, 출발지 EKS · 목적지 RKE2의 Collector. T7의 앱 배포 방식이 정해지면 그 방식을 따른다. 지금 T7 README는 "production application deployment"를 남은 일로 둔다.
- **Tailscale ACL:** Collector 태그 → 플랫폼 지표 저장소 포트만 허용 (T30 tailnet ACL과 함께).
- **PgBouncer · CNPG 지표 수집 대상:** 배포된 Service 이름이 정해진 뒤 `collector.yaml`에 추가.
- **콘솔용 조회 API:** 환경별 요청 · 오류 · p95, 최근 수신 시각.
- **T34와 협의:** 목적지 Collector를 로그 파이프라인과 하나로 둘지.
