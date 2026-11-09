# AWS network module

설계 문서 8.8의 `network/aws` 구현이다. 출발지·플랫폼 VPC, 사설 서브넷, 피어링, SG, 암호화한 Flow Logs를 제공한다. 두 VPC의 연결을 한 상태에서 관리해 피어링 소유권을 나누지 않는다.

실행은 [`../../../envs/aws-foundation`](../../../envs/aws-foundation)에서 한다. 이 모듈은 backend와 자격증명을 선언하지 않으며, root provider의 계정 경계와 `component` 등 기본 태그를 상속한다. 출력 `foundation`은 다음 환경의 입력 계약이다. 노드·클러스터·외부 egress는 T6·T7의 범위다.

`terraform init -backend=false && terraform test`는 실제 클라우드 생성 없이 경계·입력 오류를 검사한다. 실제 AWS 검증은 환경 root의 `verify.py`로 수행한다.
