# T37 실제 플랫폼 지표 저장소 부분 인수

2026-11-06 01:58 KST. T37 #104의 플랫폼 저장소 배포 범위를 실행했다.
**실제 AWS 플랫폼 RKE2 시험이며, 양쪽 Collector와 tailnet 경로 인수는 아니다.**

## 적용과 확인

- 개인 SSO와 개인 RKE2 인증서로 등록된 플랫폼 클러스터 UID를 대조했다.
- 기존 관측 수신 Service UID를 인계값과 대조했다. 그 Service를 유지했다.
- 변경 전에는 Prometheus Deployment와 TLS Secret이 없고 준비된 endpoint가 0개였다.
- `platform-store` 묶음으로 조회 API 네 리소스를 제외했다. 나머지 8개 리소스는
  기존 `platform` 렌더링과 동일함을 확인했고, 서버 모의 적용을 통과했다.
- 운영 관측 CA는 로컬에서 새로 발급했다. 기존 운영 CA/Secret은 확인되지 않았으며
  임시 로컬 시험 CA는 재사용하지 않았다. CA 개인 키는 클러스터에 전달하지 않았다.
- 서버 TLS Secret과 고정 digest의 Prometheus를 배포했다.
  Ready replica 1개, 기존 수신 Service의 Ready endpoint 1개를 확인했다.

## 실제 서버 mTLS 시험

개인 Kubernetes 인증의 loopback port-forward로 실제 서버에 요청했다.
이 접근은 NetworkPolicy와 tailnet 전송 검증을 대신하지 않는다.

| 요청 | 결과 |
|---|---|
| 허용된 조회 인증서로 query API 접근 | TLS 1.3, HTTP 200 |
| 같은 인증서로 readiness 접근 | TLS 1.3, HTTP 200 |
| 클라이언트 인증서 없음 | TLS 거부 |
| 같은 CA지만 허용되지 않은 SAN의 인증서 | TLS 거부 |
| 정상 클라이언트 인증서, 잘못된 서버 이름 | 인증서 검증 거부 |

수신 Service가 연결할 준비된 endpoint는 생겼지만, 이 시험에서 실제 Collector 지표는
수집하지 않았다. 빈 query 응답을 환경 정상이나 지표 수집 성공으로 해석하지 않는다.

## 남은 인수

- T7/T30의 양쪽 전용 경로와 tailnet grant 적용·허용/거부 확인
- 환경별 Collector 인증서 전달 및 실제 앱·PgBouncer·CNPG 지표 수집
- 사칭 지표 거부, 한쪽 수집 중단 시 확인 불가 표시
- 검증된 이미지의 콘솔 조회 API 배포 및 콘솔 연결
- 저장소 영속성: 현재는 기존 emptyDir 구성이다. Pod 재생성 시 보존되지 않으며
  프로덕션 영속 운영 자격을 충족했다는 의미가 아니다.

T37 체크리스트는 이 부분 인수만으로 완료 처리하지 않는다. 관측 저장소는 유지하며,
이번 검증의 로컬 터널과 port-forward는 종료한다. 공유 프록시·DB·앱 설정은 변경하지 않았다.

정확한 소스는 [source-manifest.json](source-manifest.json), 공개 결과는
[summary.json](summary.json)에 있다. 등록 식별자·발급 키·원본 readback은 비공개로 보존한다.
