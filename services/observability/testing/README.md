# 관측 조회·전송 검증

기존 `run.sh`, `k8s.sh`, `rotation.sh`는 각각 Compose·k3s·인증서 교체를 확인한다.
팀 환경 권한이나 운영 성공을 대신하지 않는다.

## 오류율 쿼리

```sh
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD/testing:/test:ro" prom/prometheus:v3.15.1 \
  test rules /test/http_queries_test.yml
```

`services/observability`에서 실행한다. 정상 응답만 존재하면 오류율 0,
5xx가 있으면 실제 비율, 시계열이 없으면 결과 없음, 요청이 없으면 수치로 해석할 수 없음,
카운터 재시작을 시험한다. pytest는 시험식과 API가 실제 사용하는 식의 일치도 확인한다.
전체 쿼리 결과를 무조건 `or vector(0)`으로 바꾸지 않는다.

## 실제 앱 연결

`demo-chat` PR #14의 `testing/observability_e2e.py`가 실제 앱 두 개·PostgreSQL·
OTel Collector·Prometheus·T36 HTTP intake를 연결한다. 앱 수신과 플랫폼 전송은
서로 다른 CA의 mTLS를 사용한다. T18 부하 생성기와 T36 intake checkout이 필요하다.
Nginx는 Cloudflare의 시험 대역이고 DB는 하나이므로, 클라우드/DB 컷오버 증거가 아니다.
