# T7: 실행마다 수동 토큰을 전달하지 않는 목적지 인증

2026-11-06 AWS RKE2의 격리 Pod에서 Mac ARM 목적지 API로 실행했다.
[결과](results.json)는 운영 식별자를 제외한 요약이며 비공개 원본의 해시를 포함한다.
표준 exec 시험에 공급한 모듈 바이트를 보존하고 커밋의 파일과 일치함을 확인했다.
설치 작업은 커밋 전 운영 어댑터로 실행했으므로 모든 설치 동작을 최종 커밋 실행으로 표현하지 않는다.

| 실제 확인 | 결과 |
|---|---|
| 자동 발급 | 저장된 bearer 없이 워크로드 태그 신원으로 두 번 발급. 실제 목적지 ServiceAccount username·UID 일치 |
| 표준 클라이언트 | 서로 다른 `kubectl` 프로세스 두 개가 `ExecCredential v1`로 인증. 운영 Secret·노드 조회는 403 |
| 발급 제한 | Secret 결속 누락·다른 audience·긴 수명은 admission 거절. 다른 writer 계정 발급은 RBAC 거절 |
| 회수 | 독립 시험 계정의 결속 Secret을 UID 삭제한 뒤 10.16초 안에 TokenReview 인증 거부. 같은 이름 재생성·옛 UID 재발급도 거부 |
| 정리 | AWS 시험 Pod·ConfigMap과 목적지 회수 시험 Secret·ServiceAccount를 UID로 삭제하고 부재 확인 |

처음 다섯 AWS 경로 시험은 실패했다. 인증서 준비 전 서비스 광고가 보류되는 순환 대기와,
처음 조회에 실패한 egress 주소를 나중에 재조회하지 않는 Tailscale 1.104.4 경로를 확인했다.
등록된 issuer 설정을 UID·resourceVersion으로 대조해 광고하고, Ready 이후 egress ConfigMap의
전용 표식으로 기존 파일 감시를 호출했다. TLS를 끄거나 프록시 Pod를 재시작하지 않았다.
이 설치 순서와 조건부 복구를 [설치 모듈](../../../infra/envs/rehearsal-access/credential_install.py)에 남겼다.

이 시험은 실제 소비 Pod의 표준 exec 호출과 회수 검증이다. 9분 캐시 만료 대기,
T15 후보 앱의 DB 접속, 실제 AI 제안에서 승인 대기까지의 통합 결과는 포함하지 않는다.
기존 T15 단일 담당이 같은 등록 설정을 호스트에 조립해 이어서 확인한다.
