# T7: AWS → 목적지 API 실제 연결과 권한 검증

11월 5일 16:29–16:41 KST, 등록된 AWS RKE2의 별도 Pod 두 개에서 등록된 목적지 RKE2로 실행했다. 개인 SSO와 양쪽 cluster UID를 확인하고, T15 실행 신원과 T31 배포 신원을 각각 사용했다. [결과 요약](results.json)은 비공개 원본에서 운영 식별자를 제외한 요약이다. 원본 영수증과 실제 실행한 미커밋 스크립트의 해시는 함께 기록했다. 나중에 추가한 생성기의 커밋을 과거 실행 소스로 표시하지 않았다.

| 확인 | 결과와 범위 |
|---|---|
| 실제 네트워크 | 두 AWS Pod의 TLS 검증 성공, 무자격 Kubernetes 401. 네트워크 경로 확인이며 권한 승인과 구별한다. |
| T15 실행 신원 | 18개 검사 PASS. 실제 ConfigMap 생성·조회·UID 삭제. Secret 목록·다른 namespace·운영 앱 patch·impersonation·privileged Pod 거절. Pod create/exec/log는 권한 API에서 허용을 확인했으며 실제 exec 시험은 아니다. |
| T31 배포 신원 | 20개 검사 PASS. 독립 replicas=0 시험 Deployment GET/PATCH/GET, 대상 namespace Pod list 성공. 다른 Deployment patch·Secret·타 namespace·impersonation 거절. 운영 앱 rollout을 수행하지 않았다. |
| 역할 간 네트워크 | 자기 경로의 TLS/401 성공 전후로 상대 프록시 IP 접근을 3회씩 시도해 모두 timeout. DNS·TLS·HTTP 오류를 네트워크 차단으로 집계하지 않았다. |
| 정리 | AWS Job 8개와 임시 token Secret 2개 UID 삭제, Pod 잔류 0. 목적지 apply 시험 namespace UID 삭제와 부재 확인. T3 인수용 namespace는 소유와 UID를 기록해 유지했다. |
| 상태 관측 | 실제 통신이 성공한 뒤에도 apply Service Ready=False가 남았다. 동일 Service의 메타데이터를 재조정하고 16:41:02 KST Ready=True를 확인했다. Pod·DB 연결은 재시작하지 않았다. |

첫 실행은 egress ProxyClass에 API 프록시 전용 이미지를 지정해 준비 단계에서 실패했다. `tailscale/tailscale:v1.104.4`를 digest에 고정한 뒤 새 Job으로 다시 검증했다. 실패 영수증을 보존했으며, 동일 Job 성공으로 덮어쓰지 않았다. 토큰은 1시간 TokenRequest로 발급해 메모리에서 암호화하여 공급했고, 평문 파일·로그에 남기지 않았다.

이번 실측은 T3 전체 격리, T15 실제 모델 리허설, T31의 실제 앱 배포, T25 컷오버 성공을 증명하지 않는다. 개인 WSL의 초대·인증서·시험 창과 운영 관리 러너의 namespace 생성·자격 1건 읽기 권한도 별도다. T7 기존 완료 기준과 후속 제품 통합 기준을 구분한다.

[재사용 설치 입력](../../../infra/envs/rehearsal-access/README.md)은 같은 역할 분리 구조를 코드로 남긴다. 생성기는 초기 통신을 닫고, operator가 만든 실제 Service 포트를 읽어 정확한 포트만 허용하도록 강화했다. 이 포트 제한 출력의 적용·실측은 위 실행에 포함되지 않으며 별도 결과가 필요하다.
