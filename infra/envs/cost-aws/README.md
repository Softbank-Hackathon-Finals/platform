# T1 월 예산과 component 비용 조회

설계 NFR-8의 비용 분리를 구현한다. 지원금은 **월 500,000원**이며 `policy.auto.tfvars.json`에 원화 금액과 환율 기준일을 함께 둔다. 2026-11-02 ECB 기준 EUR/USD 1.1216, EUR/KRW 1506.20에서 KRW/USD **1342.902996**을 계산했고, AWS 계정의 USD 비용 조회에 맞춘 월 한도는 **US$372.32**다. 환율을 실행할 때마다 바꾸지 않는다. 지원금·환율 변경은 정책 파일을 수정한 PR과 Terraform plan으로 검토한다.

| 범위 | 관리 방식 |
|---|---|
| 월 예산 | 계정 전체 UnblendedCost, 실제 80%/100% 및 예상 100% 초과 경보 |
| 비용 귀속 | `project=bravo-platform`, `component=source/platform/target` |
| 공통 자원 | 피어링, 로그용 공통 IAM/KMS, Terraform state는 platform |
| 수신자 | 운영자 본인만. 주소는 무시되는 operator.auto.tfvars에 저장 |
| 상태 파일 | 기존 암호화 S3 backend의 별도 `cost/aws.tfstate` |
| 비밀 관리 | AWS 인증은 기존 CLI 프로필. 키·토큰·state는 커밋하지 않음 |

계정 전체 경보에는 다른 프로젝트의 비용도 포함된다. 새 태그의 반영 지연·미태깅 비용이 경보에서 빠지지 않게 하는 보수적인 상한 알림이다. GCP/다른 계정 비용을 합산한 지원금 통합 정산은 아니다. 비용 조회에서는 팀 프로젝트, 다른 프로젝트, 미귀속을 구분한다.

## 적용과 검증

```sh
cp operator.auto.tfvars.example operator.auto.tfvars
# 본인 계정 ID와 이메일을 입력한다. backend JSON은 개인 보관 경로에 둔다.
make check
make init BACKEND_CONFIG=/private/path/cost.backend.json
make plan
# plan을 확인한 뒤
make apply
make verify EMAIL=owner@example.com EVIDENCE=/private/path/budget-verify.json
terraform plan -detailed-exitcode  # 0이면 drift 없음
```

AWS가 새 이메일 수신자를 `inactive`로 만들면 메일 인증을 완료해야 알림을 받는다. `verify.py`는 예산·경보·수신자·태그 설정을 검사하고 수신자 상태를 별도 기록한다. 설정 검증 통과는 알림 메일 배달 성공을 뜻하지 않는다. 필요하면 `aws notificationscontacts send-activation-code --arn <조회한 contact ARN> --region us-east-1`로 재발송한다. 인증 코드는 코드·로그·증빙에 저장하지 않는다.

`component` 비용 배분 키 활성화는 이 루트가 관리한다. 키는 계정 전체에 영향을 주므로 삭제 계획에서 비활성화되지 않도록 `prevent_destroy`를 둔다. 예산을 제거할 때도 비용 배분 키는 유지하는 변경을 먼저 검토한다.

`project` 키는 2026-11-03 검증 시 AWS Billing 목록에 아직 나타나지 않았다. 키가 목록에 나타난 뒤 `activate_project_tag=true`를 운영자 변수에 기록하고 plan/apply한다. 아직 없는 키를 활성화했다고 보고하거나 미귀속 비용을 0으로 표시하지 않는다. AWS는 태그 생성 후 목록 반영, 활성화 후 비용 반영에 각각 최대 24시간이 걸릴 수 있다고 안내한다. 과거 비용의 자동 소급 귀속은 가정하지 않는다.

## 비용 조회

레포 루트에서 실행한다. Python 3.10+와 AWS CLI만 필요하다.

```sh
python3 scripts/cost_report.py --account-id 123456789012 \
  --start 2026-11-01 --end 2026-11-03 --output /private/path/cost-report.json
```

시작일은 포함, 종료일은 제외하며 UTC의 오늘 이후는 거부한다. 계정 확인 후 Cost Explorer를 페이지 끝까지 읽는다. API 오류, 기간 누락, 중복 그룹, 다른 통화는 오류로 끝낸다. JSON 금액은 Decimal 문자열이며 원화 값은 고정 환율을 사용한 참고치다.

| 출력 | 의미 |
|---|---|
| account_components | 계정 전체에서 관측된 source/platform/target 비용. 다른 프로젝트도 포함 가능 |
| components | `project=bravo-platform`까지 확인된 각 component 비용 |
| project_unattributed | project 키로 귀속하지 못한 금액 |
| bravo_unknown_component | 팀 project는 확인됐으나 component가 없거나 계약 외 값인 비용 |
| other_projects | 다른 project 값에 귀속된 합계. 이름은 출력하지 않음 |
| account_total | 위 팀 귀속·미귀속·다른 프로젝트의 합계 |
| estimated / tag_status | AWS 추정 여부와 키 활성화 상태 |
| null / observed=false | 해당 component 관측 없음. 0원이 아님 |

원시 비용 결과는 개인 경로에 보관한다. PR에는 검사 통과 여부·귀속 가능 여부와 필요한 최소 증빙만 올린다.

- [AWS 비용 배분 태그 활성화](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/activating-tags.html)
- [AWS 예산 이메일 인증](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-email-recipients.html)
- [AWS 예산 평가 지연](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html)
- [ECB 환율 원본](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml)
