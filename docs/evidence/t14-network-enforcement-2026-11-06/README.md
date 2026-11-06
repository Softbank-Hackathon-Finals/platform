# T14 출발지 EKS 네트워크 정책 집행 확인

2026-11-06 14:10:56–14:12:02 KST. 실제 출발지 EKS의 **별도 시험 namespace**에서 확인했다.
운영 앱·DB·정책은 변경하지 않았고 시험 자원 다섯 개와 이번 SSM 터널은 정리했다.

## 관측

- VPC CNI 1.23.2 / network policy agent 1.4.3. `--enable-network-policy=true`,
  `NETWORK_POLICY_ENFORCING_MODE=standard`; 정책 이벤트 로그는 비활성 상태였다.
- 같은 노드에 고정 digest의 이미지로 Deployment 세 개를 준비했다. 각 Pod의
  ReplicaSet → Deployment UID 소유권을 확인했다. SA 토큰 미장착, 비특권·읽기 전용
  루트 파일시스템·리소스 상한·restricted Pod Security를 사용했다.
- 대상은 시험 TCP 8080 listener다. 정책 적용 전 허용/거부 후보 두 주체 모두 연결됐다.
- 시험 target Pod에만 ingress 정책을 적용하고 allowed Pod selector만 허용했다.
- 세 번 반복해 **정상 연결 → 다른 주체 3초 이상 타임아웃 → 정상 연결**을 확인했다.
  거부 경로 시간은 3,112 / 3,113 / 3,204 ms다. 정책·Pod/컨테이너 UID와 재시작 수가 유지됐다.
- source 앱 snapshot은 전후 동일했다. 생성한 Namespace·Deployment 3개·NetworkPolicy를
  UID/resourceVersion 전제조건과 foreground 삭제로 정리하고 API 404를 확인했다.

## 해석과 한계

이 시험은 제한된 Deployment 간 ingress의 실제 집행 확인이다. drop 이벤트를 직접 수집한
것은 아니므로 타임아웃 한 건만으로 정책 drop이라고 단정하지 않는다. 적용 전 연결,
선택된 정책 readback, 반복된 허용 대조군으로 정책과 일치하는 동작을 확인했다.
DB·Pooler 통제 PASS, 외부 접근 차단, 다른 노드/목적지/전체 애플리케이션 격리를 의미하지 않는다.

앞서 #436의 실제 다른 namespace→DB·풀러 TCP 연결 허용 관측은 그대로 유지한다.
그 시험은 단독 Pod였고, 여기서는 Deployment Pod를 사용했다. AWS는 단독 Pod에서
집행이 불안정할 수 있다고 설명한다. 따라서 이 결과로 앞선 실패 원인을 CNI 장애로
단정하지 않으며, 운영 정책·선택자·컨트롤러 소유 관계를 포함한 재시험이 필요하다.

참고: [AWS VPC CNI NetworkPolicy 고려 사항](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html).

원문은 비공개 보존한다. 실행 ID `c1e2a1a8-e8c8-4839-b5dd-107a4449644a`,
원문 SHA-256 `6b46fad448560a6c355e92fcfd262e815abe5dbc06b0382a41866c1ed461734d`.
계정·노드·namespace·Pod·컨테이너·인증정보 원문은 이 문서에 포함하지 않는다.
T14 완료 체크나 T19의 운영 PASS 입력으로 승격하지 않는다.
