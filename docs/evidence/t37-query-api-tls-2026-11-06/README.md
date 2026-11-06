# T37 조회 API → 콘솔 HTTPS 인수

원격 콘솔의 `T37Client`는 HTTPS를 요구하지만 기존 조회 API 배포는 HTTP였다.
`platform-api-tls` overlay와 서버 TLS 설정을 추가하고 실제 로컬 컨테이너로 연결을 확인했다.
**실제 AWS 배포 결과가 아니라 로컬 Docker 검증**이다.

## 확인 결과

- 기존 T36/T37 시험과 실제 로컬 TLS 소켓 시험: **201개 통과**.
- TLS 1.3 연결·정상 조회 성공. 잘못된 CA/서버 이름, TLS 1.2, 평문 HTTP 거부.
- 서버 TLS 설정의 디렉터리 누락·인증서/키 불일치는 시작 실패. HTTP 대체 실행 없음.
- 고정 기반 이미지로 linux/amd64 이미지를 빌드했다. 비루트 사용자 `10001:10001`,
  읽기 전용 루트·capability 제거·권한 상승 금지 상태로 API를 실행했다.
- 실제 Prometheus의 mTLS → HTTPS 조회 API → 기존 콘솔 `T37Client` 연결 성공.
- 토큰 누락·잘못된 토큰은 401. 저장소를 중단하면 503이며 정상/0으로 위장하지 않는다.
- 빈 저장소의 두 환경은 `UNOBSERVABLE`, HTTP 수치는 null로 반환됐다.
- Docker healthcheck는 loopback에서 CA/서버 이름을 검증했다.
- Kustomize 비교에서 조회 API Deployment만 변경되고 저장소·NetworkPolicy·다른 리소스는 동일했다.
- 시험별 임시 CA·토큰을 사용했다. 운영 자격을 사용하지 않았고 해당 시험 컨테이너와 키를 정리했다.

재현은 [query_api_tls.py](../../../services/observability/testing/query_api_tls.py)와
[실행 안내](../../../services/observability/testing/README.md)를 따른다.
[summary.json](summary.json)에 로컬 이미지 ID와 검사 결과를,
[source-manifest.json](source-manifest.json)에 소스 바이트 지문을 기록했다.
이미지 ID는 레지스트리 게시·서명·취약점 검사 결과가 아니다.

## 운영 인수는 별도

기존 플랫폼 Prometheus 배포는 유지하며 이번 변경으로 운영 API를 배포하지 않았다.
운영 registry@digest 발행·검사, 서버 인증서/토큰 참조, HTTPS 배포, 실제 콘솔 host 연결과
양쪽 Collector 인수가 남아 있다. 기존 내부 HTTP 시험 구성은 유지한다.
Kubernetes HTTPS probe는 서버 인증서의 신뢰를 검증하지 않으므로 TLS 인수 시험으로 세지 않는다.
T37 전체 완료·T36 판정 입력·실제 환경 통합 시연 성공을 주장하지 않는다.
