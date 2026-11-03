# T1 예산 · 비용 귀속 검증 — 2026-11-03

범위는 월 지원금 50만원의 **AWS 계정 전체 경보**, 스펙 NFR-8의 component 태그, 비용 조회다. 태그 교정 이외의 네트워크 동작 변경은 없다. 이전 T1 기반 증빙을 덮어쓰지 않는 추가 기록이다.

## 실제 AWS 결과

| 확인 | 결과 | 증거 수준 |
|---|---|---|
| 월 COST 예산 | KRW 500,000 → USD 372.32, MONTHLY | 생성 후 DescribeBudget readback |
| 경보 | 실제 80%·100%, 예상 100% 초과, 본인 이메일 1명 | 3개 알림과 각 subscriber readback |
| 수신자 인증 | active | 스팸함에서 받은 AWS 인증 메일로 활성화, API 재확인 |
| 실제 초과 경보 배달 | 미시험 | 인증 메일 수신과 예산 임계치 초과 경보는 별도 |
| component 비용 배분 키 | Active | AWS Cost Explorer API |
| project 비용 배분 키 | NOT_AVAILABLE | AWS Billing에 새 키가 아직 나타나지 않음 |
| source/platform 네트워크 태그 | 24개 자원의 tags/tags_all만 수정 | 저장된 plan 검사 + 기존 AWS 경계 readback 25개 통과 |
| state 버킷·KMS 태그 | 2개 자원의 tags/tags_all만 수정 | 실제 태그 readback 2개 통과 |
| 적용 후 drift | 3개 실행 루트 모두 0 | terraform plan -detailed-exitcode = 0 |
| 비용 조회 | 11/1 포함 ~ 11/3 제외 실 API 조회 성공 | AWS 추정 비용, component 귀속 미관측 |

초기 회사 도메인 수신자가 인증되지 않아, 사용자가 지정한 AWS 계정 Gmail 주소로 알림 3개를 교체했다. 수신 이메일·인증 코드·원시 계정 비용·Terraform state/plan은 이 디렉터리에 넣지 않았다. 인증 코드도 파일에 저장하지 않았다.

`project` 키는 Billing 목록에 나타난 뒤 활성화해야 한다. 운영 문서에 재조회와 `activate_project_tag=true` 적용 경로를 마련했다. 지금 원시 결과에서 source/platform/target 비용을 모두 0원이라고 해석하면 안 된다. 조회 도구는 미관측을 `null`, 미귀속 비용을 별도 합계로 기록한다. GCP 비용과 다른 계정을 합한 지원금 통합 정산은 이번 범위가 아니다.

## 재현과 로컬 확인

- 실행 루트: [`infra/envs/cost-aws`](../../../infra/envs/cost-aws), 모듈: [`infra/modules/cost/aws`](../../../infra/modules/cost/aws)
- AWS Terraform provider 6.71.0, Terraform 1.16.5 사용. 암호화 S3의 `cost/aws.tfstate`에 별도 상태 저장.
- Python unittest 15개: 금액 합산·credit·pagination·누락/중복·0/미관측 구별·계정 가드·경보 subscriber/threshold·인증과 배달 구분.
- Terraform 모의 시험: 예산 6개, 네트워크 6개 통과. 실행 루트 3개의 validate, 재귀 fmt, ruff, Trivy HIGH/CRITICAL 0건 확인.
- 환율 원본은 [fx.json](fx.json)에 날짜·원화/USD·계산값·ECB 원문 SHA-256을 기록했다. [aws-readback.json](aws-readback.json)은 개인 계정 비용과 리소스 식별자를 제외한 검증 기록이다.

T1 전체 완료 체크는 하지 않는다. 이 PR은 리뷰 승인 후 병합해야 하며, 사람별 IAM 배포는 팀장 사용 후 회의에서 결정한다.
