# T8 첨부 복사 Job의 S3 읽기 역할

첨부 복사 Job에 넘길 30분짜리 S3 세션을 SSO 사용자도 만들 수 있게, 출발지 첨부 버킷만 읽는 IAM 역할 하나를 만든다. SSO 세션으로 `aws sts get-federation-token`을 부르면 `AccessDenied: Cannot call GetFederationToken with session credentials`로 거부되므로, 이 역할을 맡는(`aws sts assume-role`) 경로를 쓴다. 쓰는 법은 [운영 문서 5단계](../../../docs/operations/t8-attachment-copy-job.md#실행-순서)에 있다.

**아직 적용하지 않았다.** 실제 계정에서 plan · apply · 역할 맡기를 해 본 적이 없다.

## 만드는 것

| 자원 | 내용 |
| --- | --- |
| IAM 역할 `bravo-attachment-copy-reader` | 신뢰: 정확한 `AWSReservedSSO_BravoDevelopmentAdministrator_*` 역할 하나의 `sts:AssumeRole`만. 최대 세션 1시간 |
| 인라인 정책 `t8-attachment-copy-read` | 버킷 `s3:ListBucket`, 객체 `s3:GetObject`, 앱 KMS 키 `kms:Decrypt`(`kms:ViaService`가 그 리전의 S3일 때만). 그 밖의 동작과 다른 버킷은 명시적으로 거부 |

- **권한은 Job의 세션 정책과 같다.** `attachment_job.py source-policy`가 내는 문서와 같아야 하고, 기준은 [`tests/session-policy.json`](tests/session-policy.json) 하나다. 이 스택의 시험과 `scripts/tests/test_attachment_job.py`가 양쪽에서 이 파일과 비교한다.
- **운영자는 역할을 맡을 때도 그 세션 정책을 붙인다.** 세션 권한은 역할 정책과 세션 정책의 교집합이므로, 둘 중 하나만 넓어져도 세션은 넓어지지 않는다.
- **SSO 세션에서 역할을 맡는 것은 역할 체이닝이라** AWS가 1시간으로 제한한다. 운영 문서는 1800초를 요청한다.
- **태그:** `project=bravo-platform`, `component=platform`, `purpose=t8-attachment-copy`, `managed-by=terraform`. IAM은 비용이 없지만 `component`를 `scripts/cost_report.py`의 계약 값(source · platform · target) 밖으로 두지 않았다. 복사 Job은 이전 도구라 [`loadgen-aws`](../loadgen-aws/README.md)처럼 platform으로 둔다.

**갖지 않는 것:** 버킷 · KMS 키(`source-workloads`), SSO 권한 집합 · 그 IAM 역할 · 할당(Identity Center). 계정과 SSO 역할은 data로 읽기만 한다. 앱 키의 키 정책은 계정 IAM에 위임하고(`EnableAccountIAM`) 버킷 정책은 TLS가 아닌 요청만 거부하므로, `source-workloads`를 바꾸지 않아도 이 역할 정책으로 읽을 수 있다. 코드로만 확인했다.

**권한 집합 단위로 신뢰한다.** 그 권한 집합이 할당된 사람은 모두 이 역할을 맡을 수 있다. 각자 자기 SSO 세션으로 맡으므로 CloudTrail의 `AssumeRole` 기록에 원래 신원이 남고, 첨부 객체 읽기는 `source-workloads`의 CloudTrail 데이터 이벤트에 남는다.

## 비공개 입력

다섯 개 모두 필수이고 기본값이 없다. 저장소 밖의 tfvars에 둔다. 형식은 [`operator.tfvars.example`](operator.tfvars.example)이고 값은 가짜다.

| 입력 | 검사 |
| --- | --- |
| `account_id` | 버킷 · 키 · SSO 역할이 있는 계정. provider의 `allowed_account_ids`와, plan 때 호출자 계정으로 한 번 더 확인 |
| `region` | 버킷과 키의 리전. KMS 조건의 `s3.<리전>.amazonaws.com`이 된다 |
| `bucket_arn` | 출발지 첨부 버킷 하나. 객체 경로 · 와일드카드는 거부 |
| `kms_key_arn` | 그 버킷을 암호화하는 앱 키. 같은 계정 · 같은 리전의 `key/` ARN만. alias는 거부 |
| `trusted_role_arn` | `BravoDevelopmentAdministrator` 권한 집합의 IAM 역할 ARN(예약 경로 · 접미사 포함). IAM이 같은 ARN을 돌려줘야 plan이 진행된다. 다른 권한 집합 · 일반 역할 · 사용자 · STS 세션 · 와일드카드는 거부 |

`bucket_arn`과 `kms_key_arn`은 5단계 세션 정책에 넣는 값과 같다(`source-workloads`의 비공개 출력과 KMS alias). 역할 ARN은 사람의 STS 세션 ARN을 고쳐 쓰지 말고 IAM에서 읽는다. Identity Center가 `us-east-1`에 있으면 경로에 리전이 없고, 두 형식 모두 받는다. [AWS 역할 ARN 형식](https://docs.aws.amazon.com/singlesignon/latest/userguide/referencingpermissionsets.html)

## 상태와 적용

기존 암호화 S3 backend의 별도 key `runtime/attachment-copy-reader/terraform.tfstate`를 쓴다. 다른 스택의 key로 init하지 않는다. backend 설정(버킷, 리전, `encrypt=true`, KMS 키, `use_lockfile=true`) · tfvars · plan · 출력은 `$PRIVATE_DIR`(저장소 밖 개인 디렉터리)에 둔다.

```sh
terraform init -backend-config="$PRIVATE_DIR/attachment-copy-reader.backend.hcl"
terraform plan -var-file="$PRIVATE_DIR/attachment-copy-reader.tfvars" -out="$PRIVATE_DIR/attachment-copy-reader.tfplan"
```

plan에는 역할 하나와 인라인 정책 하나의 생성만 있어야 한다. 같은 이름의 역할이 이미 있으면 apply가 실패하고 덮어쓰지 않는다. 검토한 뒤 별도 승인으로 적용하고, 역할 ARN을 출력하지 않고 변수로만 받는다.

```sh
terraform apply "$PRIVATE_DIR/attachment-copy-reader.tfplan"
export SOURCE_READER_ROLE_ARN="$(terraform output -raw reader_role_arn)"
```

**권한 집합 역할이 바뀌면 다시 적용한다.** 권한 집합의 할당을 모두 지우면 그 IAM 역할이 지워지고, 다시 할당하면 접미사가 다른 역할이 생긴다. 같은 ARN으로 다시 생겨도 신뢰 정책은 예전 역할의 내부 ID를 가리킨다. 어느 쪽이든 맡기가 거부되므로, IAM에서 ARN을 다시 읽어 tfvars를 맞추고 plan · apply한다.

**정리:** 데모 뒤 같은 backend로 `terraform destroy`. 역할과 인라인 정책만 지워진다.

## 로컬 시험

```sh
terraform fmt -check -recursive
terraform init -backend=false -input=false -lockfile=readonly
terraform validate
terraform test -no-color
```

모의 provider와 가짜 계정 · ARN만 쓴다. 16개 run이 신뢰 주체 · 동작 · 세션 길이, 정책이 세션 정책 기준 파일과 같음, 호출자 계정 불일치, IAM 경로 불일치, 다른 권한 집합 · 다른 계정 · 일반 역할 · 사용자 · 세션 · 와일드카드, `us-east-1` 경로 형식, 버킷 와일드카드 · 객체 경로, 키 alias · 다른 리전 · 다른 계정, 빈 리전을 본다. AWS를 부르지 않으므로 실제 역할 맡기, IAM 정책 평가, KMS 복호화는 재지 않는다.
