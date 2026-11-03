# 일회 AWS CI 검증 호스트

T7의 CI 실행 및 T11/T15 접점을 AWS에서 시험하는 임시 환경이다. 운영 플랫폼·EKS·목적지 RKE2를 대체하지 않는다. 기존 foundation과 연결하지 않는 VPC에 Ubuntu 24.04 arm64 `t4g.medium` 한 대를 만든다. SG 인바운드는 없고 SSM으로만 관리한다. 호스트의 HTTPS 설치 경로와 검사 Pod의 NetworkPolicy 경계를 구분한다.

노드는 k3s `v1.35.5+k3s1`, Python 3.12, uv 0.9.5를 준비한다. k3s 설치 스크립트는 SHA-256으로 검증하며, 설치기가 release binary checksum을 확인한다. 모델 키·운영 자격증명은 이 호스트 IAM 역할에 없다. 모델 워커가 쓰는 자격증명을 검사 Pod로 전달하지 않는다.

`AmazonSSMManagedInstanceCore`에는 Parameter Store 읽기가 포함되므로 별도 명시적 Deny로 Parameter 값·Secrets Manager 값·KMS 복호화를 막는다. SSM 관리 통신만으로 운영/모델 시크릿 읽기 권한이 생기지 않도록 한다.

## 실행과 정리

Terraform 1.16.5를 사용한다. 상태·plan·입력 파일은 저장소 밖의 권한 0700 디렉터리에 둔다. 아래 예의 경로는 본인 배포마다 구분한다. 계정·기존 인스턴스·예산을 확인하고 AMI를 Canonical 공개 SSM parameter에서 읽은 뒤 owner `AWS_ACCOUNT_001`, `arm64`, `available`을 대조한다.

```sh
export TF_DATA_DIR="$HOME/.bravo/ci-validation-20261103/tfdata"
terraform init -backend-config="path=$HOME/.bravo/ci-validation-20261103/terraform.tfstate"
terraform fmt -check
terraform validate
terraform test
terraform plan -var-file="$HOME/.bravo/ci-validation-20261103/operator.tfvars" -out="$HOME/.bravo/ci-validation-20261103/apply.tfplan"
terraform apply "$HOME/.bravo/ci-validation-20261103/apply.tfplan"
terraform output -json validation
# SSM: AWS-RunShellScript, 위 출력의 instance_id만 대상. 준비 완료 파일: /opt/bravo-validation/ready
terraform plan -destroy -var-file="$HOME/.bravo/ci-validation-20261103/operator.tfvars" -out="$HOME/.bravo/ci-validation-20261103/destroy.tfplan"
terraform apply "$HOME/.bravo/ci-validation-20261103/destroy.tfplan"
```

입력 예: `account_id`, `ami_id`, `deployment`, `expires_at`(RFC3339), 선택 `ttl_minutes`(15~90, 기본 90). 이 값들은 운영자 파일에 두고 커밋하지 않는다. AMI 조회 parameter는 `/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id`이다. 실제 사용한 AMI는 증거에 기록한다.

cloud-init은 가장 먼저 종료 타이머를 등록한다. 기본 90분 뒤 인스턴스가 종료되며 root disk도 삭제된다. 타이머는 VPC·IAM을 지우지 않으므로 검증이 끝나면 위 destroy 계획으로 **이 state의 자원만** 정리한다. 다른 배포 state를 연결하거나 임의 태그 검색으로 일괄 삭제하지 않는다. CPU credit는 standard로 고정한다.

## 검증 범위

- Terraform mock: 네트워크·IAM·IMDS·수명·디스크 경계와 잘못된 입력을 확인한다.
- AWS: SSM 준비, k3s 버전, T3 네임스페이스 격리의 양성 대조/차단 시험, 제한된 CI fixture 실행을 확인한다.
- 실제 모델 호출·T10 승인·T31 배포는 별도 결과다. k3s 시험만으로 RKE2 CIS나 T7 운영 배포 완료라고 하지 않는다.

원본: [K3s release](https://github.com/k3s-io/k3s/releases/tag/v1.35.5%2Bk3s1), [SSM 통신 전제](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-prerequisites.html), [Canonical AMI 찾기](https://ubuntu.com/aws/docs/aws-how-to/instances/find-ubuntu-images/).
