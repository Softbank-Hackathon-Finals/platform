# Terraform 공유 상태 저장소

팀장이 한 번 만든다. 팀 `platform`용 S3 버킷은 버전 관리·KMS 암호화·공개 접근 차단·TLS 강제를 사용한다. 환경별 backend는 S3의 네이티브 lockfile을 켠다. 자격증명과 모델 키를 이 버킷이나 Terraform state에 넣지 않는다.

```sh
cp operator.auto.tfvars.example operator.auto.tfvars
# account_id를 본인 계정으로 수정
terraform init
terraform fmt -check
terraform validate
terraform plan -out=apply.tfplan
terraform apply apply.tfplan
terraform output -json backend
```

출력은 비밀이 아닌 backend 설정이다. 소비 환경마다 별도 `key`를 추가한다. 예를 들어 `foundation/aws.tfstate`와 `validation/<run>/aws.tfstate`는 분리한다. S3·KMS 접근 권한은 팀원 초대 시 별도 정책으로 부여한다. 현재는 팀장 계정으로 먼저 검증하며 팀원 배포는 회의 후 결정한다.

bootstrap은 자기 자신을 만들기 때문에 최초 state가 로컬이다. 적용 후 이 폴더에 다음 선언을 담은 `backend.local.tf`를 만들고, **같은 버킷의 `bootstrap/state-backend.tfstate`**를 지정해 `terraform init -migrate-state`로 옮긴다. `backend.local.tf`는 개인 설정이며 Git에서 제외된다. 이후 노트북의 state 사본 대신 S3의 잠긴 상태를 사용한다.

```hcl
terraform {
  backend "s3" {}
}
```

```sh
terraform output -json backend | jq '. + {key: "bootstrap/state-backend.tfstate"}' > bootstrap.backend.json
terraform init -migrate-state -backend-config=bootstrap.backend.json
```

이미 이 저장소가 준비돼 있다면 다시 bootstrap하지 않고 동일한 backend 설정으로 `terraform init`한다. 네트워크 환경은 출력에 자기 state key를 추가해 사용한다.

버킷은 `force_destroy=false`이므로 상태가 남아 있으면 삭제되지 않는다. 종료할 때는 소비 환경들을 먼저 지우고, 상태·버전 이력을 암호화해 보존한 뒤 운영자가 별도로 비운다. 키는 30일 삭제 대기다. 임의 `terraform destroy`로 다른 환경의 상태를 한꺼번에 잃지 않도록 한다.
