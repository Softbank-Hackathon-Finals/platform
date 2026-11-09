# T24 플랫폼 감사 로그 저장소 (S3 Object Lock)

`services/audit-log`(bravo-audit)가 쓰는 플랫폼 계정의 감사 버킷과 역할을 만든다. 기록 형식과 판정은 [ADR-0020](../../../docs/adr/0020-platform-audit-ledger.md)과 [서비스 README](../../../services/audit-log/README.md)를 따른다. 이 state는 버킷 · KMS 키 · 역할만 소유하고, 기록 객체는 소유하지 않는다.

| 자원 | 내용 |
|---|---|
| 버킷 | `<name>-<account>`. 생성 시 Object Lock, 버전 관리, 기본 보존 COMPLIANCE `retention_days`(데모 1일), 공개 차단, 전용 KMS 키 SSE-KMS, `force_destroy=false` |
| 버킷 정책 | 권한을 주지 않고 빼기만 한다. 평문 HTTP 거부. 기록 삭제(`DeleteObject*`)와 보호 변경(정책 · Lock 설정 · 버전 관리 · 암호화 · 공개 차단)은 `custodian_role_arns` 외 전원 거부. `BypassGovernanceRetention`은 전원 거부 |
| 쓰기 역할 `<name>-writer` | `audit/` 접두사에 Put · Retention 지정 · readback · 목록 조회만. 삭제 · 보호 변경 없음. `writer_principal_arns`만 맡을 수 있다 |
| 검색 역할 `<name>-reader` | `audit/` 접두사의 읽기 · 목록 조회만 |

## 적용

입력은 저장소 밖의 개인 파일에 둔다(`operator.tfvars.example` 참고). backend는 팀 공유 상태 버킷에 이 스택 전용 key를 쓴다(예: `platform/t24-audit.tfstate`, `encrypt`, KMS, `use_lockfile`).

```sh
terraform init -backend-config=/private/path/backend.hcl
terraform plan -var-file=/private/path/t24.tfvars.json -out=/private/path/t24.tfplan
terraform apply /private/path/t24.tfplan
terraform output -json audit_registration   # 버킷 · 역할 ARN. 공개 문서에 붙이지 않는다
```

적용자는 `custodian_role_arns` 중 하나여야 한다(precondition). 정책 적용 뒤에는 관리자 역할만 정책을 고칠 수 있기 때문이다.

## 정리

COMPLIANCE 보존 중인 객체는 누구도 지울 수 없다. 데모 뒤 정리는 보존 기간(데모 1일)이 지난 다음 관리자 역할로 객체 버전을 지우고 `terraform destroy`한다. KMS 키는 30일 삭제 대기다. Object Lock은 S3 API 계층 보호라 보존 만료 뒤 삭제와 계정 루트의 버킷 삭제는 막지 못한다.

## 검증

`terraform fmt -check`, `terraform validate`, `terraform test`(모의 provider로 정책 · 역할 경계 4종). 실제 계정 적용과 실측 결과는 [증빙](../../../docs/evidence/t24-audit-live-2026-11-06/README.md)에 있다.
