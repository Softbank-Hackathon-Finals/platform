# T19 플랫폼 게이트 서명기 (ADR-0038)

게이트 envelope을 서명하는 Ed25519 KMS 키 6개(용도마다 하나)와, 그 키로만 서명하는 workflow host 역할을 만든다. 비밀 값은 없다. 키는 KMS 밖으로 나오지 않는다.

| 자원 | 내용 |
|---|---|
| `aws_kms_key.gate[용도]` | `ECC_NIST_EDWARDS25519` · `SIGN_VERIFY`. 계정 관리자는 관리만 하고 서명하지 못한다. 서명 역할만 `ED25519_SHA_512`·RAW로 서명하고, 그 밖의 서명은 거부한다 |
| `aws_iam_openid_connect_provider.platform` | 플랫폼 클러스터의 공개 ServiceAccount 발급자(`oidc-issuer` env `platform`). audience `sts.amazonaws.com` |
| `aws_iam_role.signer` | `system:serviceaccount:platform-state:t10-workflow-host`만 웹 신원으로 맡는다. 권한은 키 정책에서만 온다 |
| `aws_vpc_endpoint.aws[sts·kms]` | 플랫폼 VPC 사설 경로. 노드 보안 그룹에서 443만 받는다 |

출력: `role_arn`(차트 `gateSigner.roleArn`), `keys`(`/workflow/gate-signer.json`의 `keys`), `endpoint_ips`(`infra/envs/workflow-host/network.py --aws-endpoints`).

## 적용

플랫폼 계정의 기존 암호화 S3 backend에 별도 key `runtime/platform-gate-signer/terraform.tfstate`를 쓴다. `issuer_url`은 발급자 discovery가 인터넷에서 200을 돌려준 뒤에만 넣는다(AWS가 그 주소에서 JWKS를 읽는다). 순서는 ADR-0038 4절이다.

```sh
terraform init -backend-config=<backend.hcl>
terraform plan -var-file=<operator.tfvars> -out plan.tfplan   # account_id, issuer_url, vpc_id, subnet_ids, client_security_group_id
terraform apply plan.tfplan
```

## 시험

`terraform test`(모의 provider): 용도마다 Ed25519 서명 키, 정확한 subject·audience만 맡는 역할, 관리자 서명 금지·다른 주체 서명 거부, 사설 endpoint와 그 IP 출력, https가 아닌 발급자·와일드카드 subject 거부.
