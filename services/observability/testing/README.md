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

## 자원 표본의 유효성

`BRAVO_K8S_CAPACITY=1 ./k8s.sh`는 평상·부하 구간 모두
`namespace/deployment/container`를 고정해 `stats.py`에 전달한다.
Deployment의 ReplicaSet·Pod 소유 UID, Ready 상태, Pod의 컨테이너 ID와
CRI 표본의 namespace·Pod UID·컨테이너 ID가 일치해야 한다.
여러 복제본의 합산 측정은 지원하지 않는다.

각 표본의 전후 소유 관계를 다시 읽고, 구간 전체가 같은 컨테이너여야 한다.
CPU는 CRI 누적 CPU/표본 시각의 차분으로 계산한다. 교체·누락·중복·
카운터 감소·시각 정체·수집 오류는 구간 전체를 UNKNOWN(종료 코드 2)으로
만든다. `set -e`인 k8s.sh도 이 측정 실패를 정상 완료로 처리하지 않는다.
재시작 전후의 차분을 합치거나 음수 CPU를 0으로 보정하지 않는다.

이 측정은 로컬 단일 노드 k3s의 자원 참고 자료다. 실제 출발지·목적지
배포, T36의 외부 관측 판정이나 운영 용량 보장을 대신하지 않는다.
# 조회 API의 원격 HTTPS 경로

`query_api_tls.py`는 임시 CA와 별도 Docker Compose 프로젝트에서 실제 Prometheus,
조회 API, 콘솔의 `T37Client`를 연결한다. 운영 인증서나 클라우드 자격은 필요 없다.
테스트 API 토큰은 실행 중 생성하며 공개 결과에 저장하지 않는다.

저장소 루트에서 이미지를 빌드한 뒤 observability 디렉터리에서 실행한다.

```sh
docker build --platform linux/amd64 -f services/observability/Dockerfile -t bravo-query-tls:test .
docker image inspect bravo-query-tls:test --format '{{.Id}}'
cd services/observability
uv sync --locked
# 위 이미지 ID 전체를 지정한다. 태그는 받지 않는다. output은 새 디렉터리여야 한다.
uv run python testing/query_api_tls.py --image sha256:<local-image-id> --output /private/tmp/query-tls-result
```

검사 범위: HTTPS health, 토큰 누락/오류 거부, TLS 1.3, 평문 HTTP 거부, 실제 저장소
mTLS 조회, 콘솔 어댑터의 HTTPS 읽기, 검증 healthcheck, 저장소 중단 시 503.
빈 저장소의 환경은 `UNOBSERVABLE`이며 앱 건강이나 컷오버 허가를 의미하지 않는다.
결과는 `qualification.json`, 정리는 `cleanup.json`에 기록한다. 이 스크립트가 만든
컨테이너/네트워크/볼륨만 종료 시 제거한다. 실제 AWS·온프렘 인수와는 구분한다.

