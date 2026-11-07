# 팀 인프라

설계 기준은 [`docs/design.html`](../docs/design.html) §7.2·§7.5, NFR-6·NFR-8이다. 이 폴더는 팀 `platform` 저장소의 T1 구현이며 개인 B-free의 설정·상태를 가져오지 않는다. 제품명은 Team Bravo의 기존 표기를 사용한다. `sova`는 가제다.

자원 구현은 `modules/<기능>/<provider>`, 실행과 state는 `envs`, 최초 상태 저장소는 `bootstrap`으로 나눈다(설계 §8.8).

| 경로 | 책임 | 다음 작업이 받는 것 |
|---|---|---|
| [`bootstrap/state-backend`](bootstrap/state-backend) | 버전 관리·KMS 암호화·잠금이 있는 S3 Terraform 상태 저장소 | 환경별 state key, 팀장 관리 권한 |
| [`envs/cost-aws`](envs/cost-aws) | 월 50만원 예산 경보, 비용 배분 키 활성화 | 운영자 경보와 component별 비용 조회 |
| [`envs/aws-foundation`](envs/aws-foundation) | AWS VPC 2개, 피어링, 제한된 SG, Flow Logs | T6·T7이 사용할 VPC·서브넷·SG ID |
| [`envs/onprem`](envs/onprem) | 빈 Ubuntu VM에 RKE2 CIS 프로필과 Tailscale 설치, 로컬 VM 시험, kube-bench | T3·T9·T30이 쓸 클러스터를 세우는 법과 CIS 제약 |
| [`envs/loadgen-aws`](envs/loadgen-aws) | 측정 · 데모 때만 켜는 부하 생성기 EC2. 인바운드 없음, SSM 접속 | T18 실행 위치, Tailscale 태그와 DB 경로 결정 사항 |
| [`envs/source-identity`](envs/source-identity) | demo-chat 로그인용 Cognito 사용자 풀 · 공개 클라이언트(PKCE) · 부하 생성기 클라이언트(AWS 서명 관리자 흐름만). 사용자는 운영자가 CLI로 만든다 | 앱의 `AUTH_ISSUER` · `AUTH_CLIENT_ID`, 부하 생성기 `--auth-client-id`, 목적지 발급자로 옮길 대상 |

배포마다 별도 state key를 사용한다. T1·T6·T7을 하나의 거대한 state에 합치지 않는다. 계정은 `allowed_account_ids`로 제한하고, 장기 인증 키는 Terraform 입력·출력·state에 넣지 않는다. 수신 이메일은 민감 입력으로 다루며 암호화된 state에는 포함된다. 변경은 저장된 plan으로 검토하고 적용한다. 팀원은 PR에서 경로별 책임과 출력 계약을 확인할 수 있다.

네트워크 기반, 월 예산 경보 설정, component별 비용 조회를 구현했다. 메일 인증과 비용 귀속 지연은 [비용 운영 문서](envs/cost-aws/README.md)에서 별도 관리한다. EKS·EC2/RKE2, NAT/외부 API egress, Tailscale, 앱, 사람별 IAM 권한은 각 작업에서 추가한다. 개인 실험의 503/S2/SHIFT 측정은 이 구현의 검증 결과가 아니다.

PR 전에는 각 폴더의 로컬 검사와 실제 클라우드 검증을 완료하고 [`docs/evidence`](../docs/evidence)에 실행 범위를 기록한다. 모의 시험, AWS API readback, 실제 패킷·앱 검증을 구별한다.

비용 태그는 `project=bravo-platform`, `component=source/platform/target`를 사용한다. 환경별 네트워크 자원은 해당 component에, 공유 피어링·로그 IAM/KMS·state는 platform에 귀속한다. 이전 `network`/`state` 값은 이번 변경에서 교정한다. AWS가 태그를 지원하지 않는 보조 자원은 부모 자원과 같은 Terraform state로 관리한다.
