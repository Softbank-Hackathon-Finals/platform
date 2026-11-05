# T37 출발지 EKS Collector 부분 인수 · 2026-11-06

등록된 출발지 EKS에 고정 이미지의 Collector 1개를 배포하고 Ready를 확인했다.
출발지 앱의 기존 등록 Deployment UID를 대조했으며 앱·DB·기존 Tailscale 경로는 변경하지 않았다.
전체 T37 인수가 아니라 **출발지 Collector 준비와 로컬 중계 TLS 검증**이다.

## 적용

- `collector-app-mtls`의 기존 구성으로 ServiceAccount·ConfigMap·Service·Deployment·기본 NetworkPolicy를 생성했다. 이름 충돌을 확인하고 모든 객체의 서버 dry-run을 수행했다. 기존 객체를 덮어쓰지 않았다.
- Collector의 NetworkPolicy를 먼저 만들고, 전송/수신 Secret 및 설정을 넣은 뒤 Pod를 기동했다. 서비스 계정 토큰 자동 마운트 없음, non-root·read-only filesystem·capability drop을 유지했다.
- 전송은 기존 플랫폼 CA의 `collector-aws` 인증서를 사용한다. 앱 수신은 별도 CA의 TLS 1.3/mTLS로 구성했다. CA 개인 키와 시험 클라이언트 키는 클러스터에 넣지 않았다.
- 환경 설정은 새 이름의 immutable ConfigMap이다. 환경은 `aws`, 전송 목적지는 기존 전용 egress Service, TLS 서버 이름은 검증된 플랫폼 수신 이름이다. Pod의 설정 참조·이미지·Ready와 서버의 설정 hash를 읽어 대조했다.
- 기존 출발지 프록시의 차단 정책은 UID·spec 모두 동일하다. Tailscale grant나 다른 namespace의 허용 정책을 변경하지 않았다.

## 실제 수신 검증

개인 Kubernetes port-forward로 새 Pod의 4318 포트에 접속했다. 인증서는 CA·서버 이름을
검증했다. 정상 요청은 빈 OTLP `resourceMetrics`이며 앱 데이터나 공격 문자열을 보내지 않았다.

| 시험 | 결과 |
|---|---|
| 허용 클라이언트 인증서, TLS 1.3, 올바른 CA/호스트명 | HTTP 200 |
| 클라이언트 인증서 없음 | 거부 |
| 다른 서버 이름 | 인증서 검증 실패 |
| TLS 1.2 | 거부 |
| 평문 HTTP | 정상 수신 불가 |

시험 포트 포워드와 이 작업의 SSM 세션은 종료했다. 새 Collector와 준비 자원은 다음
경로 인수를 위해 유지했다. 개인 키·원문 운영 식별자·Pod 로그는 비공개로 보존한다.
[요약](summary.json)과 [적용 소스 지문](source-manifest.json)을 함께 본다.

## 남은 인수

- 프록시 제한 허용 및 실제 tailnet grant는 기존 T7 담당 경로에서 진행한다. 이번
  port-forward는 CNI/NetworkPolicy 집행이나 tailnet 허용·거부 시험을 대신하지 않는다.
- 플랫폼 실제 수신과 `collector-self`의 시각 증가, 목적지 경로, 양쪽 앱·DB·PgBouncer 지표,
  사칭 거부 및 수집 중단 시험은 미완료다. Ready를 전송 성공으로 계산하지 않는다.
- 앱 수신용 CA/클라이언트 자격과 T22 앱 설정 인수는 별도다. 앱 Deployment를 재시작하거나
  실제 앱을 새 Collector로 연결하지 않았다. DB scrape 자격과 접속 설정도 추가하지 않았다.
- 전송 큐는 기존 `emptyDir`이며 Pod 교체 때 보존되지 않는다. 관측 공백·큐 잔량 확인 없이
  재시작하지 않는다. 운영 인증서 회전·폐기 전체 자격을 취득했다는 의미도 아니다.

T37의 미완료 두 항목과 4/6 체크를 유지한다. 이 결과를 T14 전체 통제 PASS나 T36 전환
판정 근거로 사용하지 않는다.
