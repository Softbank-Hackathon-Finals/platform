# 팀 시험 API 스트리밍 연결 수정 — 2026-11-06

이전 배포 도구는 API Pod port-forward 안에서 exec를 실행했다. exec 정상 종료 뒤 외부 터널의 broken pipe/lost connection이 나면 후속 get이 연결 거부를 받았다. WSL kubectl 1.35에서 SPDY로도 반복 실행 뒤 실패한다는 보고를 수용해 중첩 터널을 제거했다.

기존 개인 auth 프록시는 토큰 발급에 사용한다. 사람 tailnet의 별도 noauth ProxyGroup을 추가하고, 기존 허용 태그의 TCP443으로 단기 member SA 토큰과 --as를 Kubernetes API에 보존한다. 기존 auth 프록시·RBAC·tailnet ACL은 바꾸지 않았다. 서비스 호스트 승인과 Ready, 인증서, 실제 API 신원을 확인했다. 운영 프로필은 기존 팀 비공개 배포 경로로 제공했다.

[실측 요약](result.json)은 실제 개발 RKE2에 macOS/ARM kubectl 1.35.0으로 실행한 결과다. WebSocket/SPDY 각각 exec 12회 및 직후 API 조회, 두 방식의 logs -f 중단 뒤 조회, 가장한 SA의 실제 Forbidden, 실패 exec exit17과 25초 idle 뒤 조회 등 57개 검사가 통과했다. 잘못된 bearer의 API 인증 거부도 별도 확인했다. Python 단위시험 9개와 Ruff가 통과했다.

시험 namespace는 UID 조건으로 삭제했다. 최초 40회 정리 조회에서는 아직 남아 있어 원 실행이 exit1로 끝났고 해당 실패 기록은 보존했다. 이후 같은 namespace를 재조회해 부재와 Pod 0개를 확인했다. 시험을 다시 실행하거나 finalizer를 제거하지 않았다. 초기 정리 timeout을 처음부터 성공한 것으로 바꾸지 않았다.

WSL 장치 자체의 수신 검증과 T3/T15 제품 전체 완료는 이 결과에 포함하지 않는다. 신규 helper는 환경변수로 프로토콜을 강제하거나 실패 명령을 재실행하지 않는다. 자격 만료와 신원·클러스터 불일치를 거부하고 자식 프로세스 및 임시 kubeconfig를 정리한다.

설치·사용: [TEAM-TEST.md](../../../infra/envs/rehearsal-access/TEAM-TEST.md).
