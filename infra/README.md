# 팀 인프라

설계 기준은 [`docs/design.html`](../docs/design.html) §7.2·§7.5, NFR-6·NFR-8이다. 이 폴더는 팀 `platform` 저장소의 T1 구현이며 개인 B-free의 설정·상태를 가져오지 않는다. 제품명은 Team Bravo의 기존 표기를 사용한다. `sova`는 가제다.

| 경로 | 책임 | 다음 작업이 받는 것 |
|---|---|---|
| [`state-backend`](state-backend) | 버전 관리·KMS 암호화·잠금이 있는 S3 Terraform 상태 저장소 | 환경별 state key, 팀장 관리 권한 |
| [`aws-foundation`](aws-foundation) | AWS VPC 2개, 피어링, 제한된 SG, Flow Logs | T6·T7이 사용할 VPC·서브넷·SG ID |

배포마다 별도 state key를 사용한다. T1·T6·T7을 하나의 거대한 state에 합치지 않는다. 계정은 `allowed_account_ids`로 제한하고, 비밀값은 Terraform 입력·출력·state에 넣지 않는다. 변경은 저장된 plan으로 검토하고 적용한다. 팀원은 PR에서 경로별 책임과 출력 계약을 확인할 수 있다.

현재 네트워크 기반만 구현했다. EKS·EC2/RKE2, NAT/외부 API egress, Tailscale, 앱, 사람별 IAM 권한, 예산 알림은 각 작업에서 추가한다. 개인 실험의 503/S2/SHIFT 측정은 이 구현의 검증 결과가 아니다.

PR 전에는 각 폴더의 로컬 검사와 실제 클라우드 검증을 완료하고 [`docs/evidence`](../docs/evidence)에 실행 범위를 기록한다. 모의 시험, AWS API readback, 실제 패킷·앱 검증을 구별한다.
