# Tailscale 피어 릴레이 EC2 (T44)

온프렘 VM과 AWS 사이의 tailnet 경로가 DERP 중계를 타지 않게, 공인 UDP 포트가 하나 열린 `t4g.micro` 피어 릴레이를 기본 VPC에 전용 보안 그룹으로 만든다. 누가 이 릴레이를 쓸지는 tailnet 정책의 `tailscale.com/cap/relay` grant가 정한다(`infra/iac/environments/platform.json`의 `tailnet-peer-relay`). 켜 두면 시간당 약 US$0.012(인스턴스 + Elastic IP)다.

## 왜 필요한가

2026-11-08 실측에서 목적지 풀러(`bravo-target-pooler-0`) → 출발지 DB(`bravo-source-db`)가 직접 연결을 맺지 못하고 DERP(tok)를 탔다. `tailscale ping` 460–770ms, 목적지 앱의 `/api/health/db` 1.1–1.2초, 새 DB 연결은 15초 connect timeout까지 났다.

- 온프렘 VM은 QEMU 사용자 네트워크 뒤라 목적지마다 포트가 바뀌는 NAT다(`netcheck` `MappingVariesByDestIP: true`).
- AWS 쪽 장치는 사설 서브넷의 NAT 게이트웨이 뒤다. Mac 호스트 ↔ AWS도 직접 연결이 맺어지지 않았다.

두 끝이 모두 이런 NAT면 hole punching이 안 된다. 공인 주소의 UDP 포트 하나를 가진 릴레이는 양쪽이 바깥으로 보내기만 하면 닿는다.

| 결정 | 이유 |
|---|---|
| 기본 VPC의 2a 기본 서브넷, 전용 보안 그룹 | 피어는 인터넷으로 닿는다. 계정이 리전 VPC 한도(5)에 차 있어 새 VPC를 만들 수 없다(2026-11-08 apply `VpcLimitExceeded`). 기본 VPC · 서브넷 · 기본 보안 그룹은 다른 워크로드가 쓰므로 이 스택은 읽기만 하고, 자기 보안 그룹만 더한다. 출발지 · 플랫폼 VPC와 그 상태 파일은 건드리지 않는다 |
| Elastic IP | 껐다 켜도 릴레이 주소가 같다 |
| 인바운드 UDP `relay_port`(기본 40000)만 | 릴레이 세션 없는 패킷은 버린다. tailscaled 자신의 41641과 겹치면 바인딩이 충돌해 변수 검사가 거부한다 |
| 아웃바운드 TCP 443 + UDP 전체 | 피어의 NAT가 정한 포트로 중계 패킷을 돌려보낸다. 제어 · DERP · SSM · apt는 443이다 |
| 인증 키 없음 | 가입은 로그인 URL을 tailnet 관리자(릴레이 태그 소유자)가 승인한다. Terraform · user data · state에 자격이 없다 |
| `--shields-up` | 중계는 UDP 전달이다. tailnet 안에서 이 호스트로 오는 연결은 없다 |
| `component=platform`, `purpose=tailnet-relay` | [`cost_report.py`](../../../scripts/cost_report.py)가 귀속하는 값이다 |

## 적용 (운영자)

정책을 먼저 반영한다. `platform.json`의 `tailnet-peer-relay`가 develop에 병합되고 `tailnet-policy` 적용이 끝나야 태그 소유자와 grant가 생긴다. 그 전에 가입하면 태그를 받을 수 없다.

```sh
cd infra/envs/tailnet-relay
aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text
terraform init -backend-config="$PRIVATE_DIR/tailnet-relay.backend.hcl"   # 기존 암호화 S3 backend, 이 스택만의 key
terraform plan -var-file="$PRIVATE_DIR/tailnet-relay.tfvars.json" -out="$PRIVATE_DIR/tailnet-relay.tfplan"
terraform apply "$PRIVATE_DIR/tailnet-relay.tfplan"
```

cloud-init이 끝나면(`/var/lib/bravo-relay-ready`) SSM으로 가입한다. `up`은 승인될 때까지 기다리므로 백그라운드로 띄우고 로그의 URL을 tailnet 관리자에게 넘긴다.

```sh
sudo sh -c 'nohup /usr/local/sbin/bravo-tailnet-join >/var/log/bravo-tailnet-join.log 2>&1 &'
sudo cat /var/log/bravo-tailnet-join.log   # https://login.tailscale.com/a/... → 관리자가 열어 승인
```

## 검증

- `terraform test`(모의 공급자): 인바운드는 릴레이 UDP 포트 하나, VPC 기본 그룹 닫힘, SSH 키 없음 · IMDSv2, 보안 그룹 하나, 가입 스크립트에 인증 키 없음, 41641 거부, 선언된 자원 목록 고정.
- 실제 효과: 릴레이 사용 태그의 장치에서 `tailscale ping <상대>`가 `via peer-relay(...)`로 바뀌고 지연이 줄어드는지 본다. 바뀌지 않으면 `tailscale debug peer-relay-servers`(릴레이 장치)와 정책 grant를 먼저 본다.

## 정리

데모 뒤 `terraform destroy`로 지운다. 이어서 `platform.json`에서 `tailnet-peer-relay`를 빼는 PR을 병합하면 `tailnet-policy` 적용이 태그 소유자와 grant를 prune한다. 관리 콘솔에서 장치도 지운다.
