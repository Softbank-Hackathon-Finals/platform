# T1 AWS 네트워크 기반

출발지와 플랫폼을 같은 계정의 서로 다른 VPC에 놓고, 플랫폼에서 출발지 API·DB로 가는 경로만 연다. 두 VPC에 각각 두 AZ의 사설 서브넷을 만든다. `component=network` 태그는 provider가 붙이고, Flow Logs는 전용 KMS 키로 암호화해 7일간 보관한다.

| 출발 SG | 목적 SG | 신규 연결 | 포트 |
|---|---|---|---|
| `platform_control` | `source_api` | 허용 | TCP 443 |
| `platform_control` | `source_db` | 허용 | TCP 5432 |
| 그 밖의 경로 | 위 SG | 거부 | 모두 |

SG는 상태를 추적하므로 허용된 연결의 응답은 돌아온다. 플랫폼 SG의 인바운드와 출발지 SG의 신규 아웃바운드는 비워 둔다. 기본 SG도 양방향으로 닫는다. 같은 리전의 피어 SG를 참조하는 방식은 [AWS VPC Peering 문서](https://docs.aws.amazon.com/vpc/latest/peering/vpc-peering-security-groups.html)를 따른다. 네트워크 허용은 DB 역할·Kubernetes RBAC·수집/Executor IAM 권한을 대체하지 않는다.

기본 대역은 `10.80.0.0/15`에서 출발지 `/16`과 플랫폼 `/16`으로 나눈다. 실제 연결 전에는 온프렘·기존 VPC·Tailscale 경로와의 중복을 확인한다. 이 단계는 IGW·NAT·노드·공인 IP를 만들지 않는다. T6·T7에서 클러스터 통신과 필요한 외부 egress를 추가해야 앱을 띄울 수 있다.

이 환경 root는 [`../../modules/network/aws`](../../modules/network/aws)를 조합한다. 재사용 자원은 모듈, 계정·태그·backend·실행 명령은 이 폴더가 맡는다. 기존 파일 경로에서 옮긴 운영자 state는 `moved.tf`로 자원 재생성 없이 연결한다.

## 작업 순서

준비: 팀 CI와 같은 Terraform **1.16.5**, AWS CLI, Python **3.9+**. 사람은 본인 인증, 자동화는 OIDC를 사용한다. 장기 키를 공유하지 않는다.

1. [`../../bootstrap/state-backend`](../../bootstrap/state-backend)의 상태 저장소를 한 번 준비한다.
2. `operator.auto.tfvars.example`을 `operator.auto.tfvars`로 복사해 계정·배포 ID·AZ·대역을 넣는다. 실제 입력 파일은 Git에서 제외된다.
3. `backend.hcl.example`을 저장소 밖의 개인 경로로 복사해 bootstrap 출력과 배포별 key를 넣는다. **서로 다른 배포에 같은 key를 쓰지 않는다.**

```sh
make check
make init BACKEND_CONFIG=/absolute/path/backend.hcl
make plan                         # 변경과 대상 계정을 검토
make apply                        # 검토한 apply.tfplan만 적용
make verify EVIDENCE=/absolute/path/readback.json
make destroy-plan                 # 검증용 배포 종료 계획 검토
make destroy                      # 검토한 destroy.tfplan만 적용
```

`check`는 모의 provider로 네트워크 경계와 잘못된 입력을 검사한다. `verify.py`는 실제 AWS에서 VPC/AZ/피어링/DNS/라우팅/SG/로그/태그를 다시 읽고 실패 시 0이 아닌 코드로 끝난다. 이것은 패킷·EKS·DB E2E 시험이 아니다. PR에는 실행 commit과 readback·삭제 증거를 함께 넣는다. 증거 JSON은 결과를 검토한 뒤 `docs/evidence`로 복사한다.

Terraform은 S3 상태를 `use_lockfile=true`로 잠근다. 이미 다른 실행이 잠금을 잡았으면 중단한다. `force-unlock`으로 다른 작업을 밀어내지 않는다. plan·state·실제 입력은 커밋하지 않는다. 사람이 변경한 AWS 설정은 다음 plan의 drift로 검토한다.

## T6·T7 출력 계약

`terraform output -json foundation`은 `vpcs`, `subnets`, `route_tables`, `security_groups`, `peering_id`, `flow_log_ids`를 제공한다. ID를 다른 파일에 수동 복사해 관리하지 말고 배포 파이프라인의 입력으로 전달한다. 각 환경 state의 전체 읽기 권한과 최소 출력 전달은 구별한다.

네트워크 기반을 삭제하면 T6·T7 소비 자원이 먼저 없어져야 한다. 로그 KMS 키는 Terraform destroy 뒤 AWS의 **7일 삭제 대기**로 남는다. 이는 실행 중인 네트워크가 남았다는 뜻은 아니다.
