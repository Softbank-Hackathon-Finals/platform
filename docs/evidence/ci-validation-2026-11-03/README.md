# AWS CI 검증 호스트와 T3 격리 시험

2026-11-03, 계정 `AWS_ACCOUNT_002`, Seoul. 코드: `infra/envs/ci-validation-aws`. 이 결과는 **AWS 위 일회 k3s 검증**이다. 운영 플랫폼 배포·RKE2 CIS·실제 모델 호출·T10 승인·T31 배포 결과는 별도로 확인한다.

## 결과

| 검증 | 결과 | 증거 |
|---|---|---|
| Terraform fmt · validate | 통과 | root와 lock file |
| Terraform mock 경계 시험 | 4 passed / 0 failed | `tests/boundaries.tftest.hcl` |
| AWS apply | 임시 root 자원 16개, 기존 foundation 변경 없음 | `deployment-readback.json` |
| AWS 재조회 plan | 변경 0 (종료 코드 0) | 운영자 경로의 `drift.log` |
| 호스트 | SSM Online, Ubuntu 24.04 arm64, k3s v1.35.5+k3s1 | `deployment-readback.json` |
| T3 양성 대조·격리 시험 | **21개 결과 모두 PASS, 종료 코드 0, 54.6초** | `t3-isolation.json` |
| IAM 정책 평가 | Parameter 값·Secrets Manager 값·KMS 복호화 explicitDeny, SSM 채널 allowed | `deployment-readback.json` |

T3 양성 대조는 같은 네임스페이스·DNS·시험용 SeaweedFS/OpenBao 대역 서버다. 거부 시험은 다른 네임스페이스 송수신, AWS 메타데이터, 인터넷, privileged/hostPath, 다른 네임스페이스 Secret, 권한 상승, NodePort, 허용 서버의 다른 포트와 다른 Pod를 포함한다. Kubernetes API 직접 접근 N5는 정본에서 정보 항목이며 이 실행에서도 막혔다. 시험용 버킷·OpenBao 경로의 서버 내부 권한은 실제 서비스가 아닌 대역 서버이므로 여기서 검증하지 않았다.

T3 코드는 팀 `develop`의 기존 `services/rehearsal-runner`에서 변경 없이 가져왔다. [소스별 SHA-256](t3-source-manifest.json)을 남겼다. 모델이나 판정 규칙을 고쳐 통과시킨 결과가 아니다.

## 예산과 권한

생성 전 실행 중 EC2가 없음을 확인했다. 예산 `bravo-platform-monthly-guard` 조회 값은 월 USD 372.32, actual USD 4.874, forecast USD 38.747였다. 이는 50만원 예산의 기존 환산 설정이며 새 환율 계산은 하지 않았다. AWS Pricing API에서 `t4g.medium` Seoul Linux OnDemand 가격 USD 0.0416/h를 확인했다. EBS·IPv4·전송료는 별도다.

SSM 관리 정책은 Parameter Store 읽기를 기본 포함하므로 명시적 Deny를 추가했다. IAM simulator 결과는 권한 평가 증거이며 실제 시크릿을 읽어 본 것이 아니다. 노드 IMDSv2 강제·hop limit 1, SG ingress 0, 임시 디스크 암호화 및 종료 시 삭제를 재조회했다.

호스트 패키지 설치/SSM 통신용 TCP 443 egress는 공개 인터넷을 사용한다. 이것과 Pod의 default-deny 경계는 다르다. Trivy AWS-0104의 이 경로에 대한 예외는 범위와 이유를 리뷰해야 하며, 검사 자체를 꺼서 통과 처리하지 않는다.

## 새 저장소 CI 코드의 AWS 실행

새 `scripts/ci_scope.py`, `scripts/ci_project.py`, 선택기 회귀와 현재 두 서비스를 같은 AWS 호스트에서 실행했다. 전송은 명시한 tracked 파일과 새 CI 파일만으로 제한했고 `.git`, `.env`, 운영자 입력, Terraform state/plan은 보내지 않았다. 호스트에서 빈 Git 인덱스를 만들고 전송한 파일만 추가했으며 remote는 없다. [전송 목록·SHA-256](ci-source-manifest.json)과 [정본 규칙 파일 추가 전송](ci-contract-source-manifest.json)을 기록했다.

| 실행 | AWS 결과 |
|---|---|
| 저장소 도구·선택기 회귀 | 16 tests 통과 |
| `ci_scope.py --full` | 현재 두 Python 프로젝트 선택 |
| `ci_project.py services/rehearsal-runner python` | locked sync · Ruff 0.17.3 · 19 tests 통과 |
| `ci_project.py services/security-findings python` | locked sync · Ruff 0.17.3 · 45 tests 통과 |
| 실제 `ci.yml` 집계 shell | scope 결과 × project 결과 × 선택 유무 32조합 모두 기대 결과 |
| Trivy 0.76.1 config HIGH/CRITICAL | `.trivyignore.yaml`의 경로 한정 예외 적용 후 잔여 0 |

첫 전송에서는 `docs/security/detection`의 JSON 규칙·골든을 빠뜨려 security-findings가 파일 누락으로 실패했다. [첫 결과](repository-ci-first-attempt.json)를 보존하고, 해당 tracked JSON 17개만 추가한 뒤 실패한 서비스만 다시 실행했다. 코드나 판정 기준을 바꾸지 않았다. 이것은 `docs/`의 JSON도 실행 의존성일 수 있다는 실제 사례이며 선택기는 그런 변경을 전체 검사로 처리한다.

최종 결과는 [repository-ci-results.json](repository-ci-results.json), 각 실행 로그는 같은 폴더에 있다. 테스트가 만든 `.venv`는 config scan에서 제외해 CI의 새 checkout과 같은 소스 범위를 검사했다. Trivy 다운로드 archive는 공식 release의 SHA-256과 대조했다. 이 실행은 유지관리되는 저장소 CI 코드의 AWS 실행이며 모델 호출·에이전트 제안 실행 증거와 구분한다.

## Bootstrap 보정

후속 T11 이미지 빌드 준비 중 Ubuntu ARM 지역 미러의 HTTPS 인증서 이름 불일치가 발생했다. TLS 검증을 끄지 않고 `ports.ubuntu.com/ubuntu-ports`로 URI를 정규화한 뒤 실제 AWS 호스트에서 패키지 설치를 통과했다. 같은 한 줄을 cloud-init에 반영했고 Terraform mock 4건·fmt를 다시 통과했다. 기존 호스트를 교체하지 않았으므로 수정된 cloud-init 전체의 새 인스턴스 부팅 시험은 미실행이다.

## 수명과 정리

인스턴스 `AWS_RESOURCE_002`, VPC `AWS_RESOURCE_007`, 배포 `20261103-s2-ci`. 실제 guest 종료 timer는 **2026-11-03 07:53:13 UTC / 16:53:13 KST**다. 타이머 종료 시 인스턴스와 디스크는 사라지지만 VPC·IAM은 남으므로 검증 종료 후 Terraform destroy가 필요하다.

운영자 상태·plan·입력은 `~/.bravo/ci-validation-20261103/`에 있고 커밋하지 않는다. **15:55 KST 정리 확인:** Terraform 16개 자원을 삭제했고 state 잔여 0개다. AWS API에서 인스턴스 terminated, VPC·서브넷·ENI·루트 EBS·IAM 역할·instance profile 부재를 확인했다. [삭제 재조회 영수증](cleanup-readback.json)을 별도로 보존했다.

```sh
cd infra/envs/ci-validation-aws
export TF_DATA_DIR="$HOME/.bravo/ci-validation-20261103/tfdata"
terraform plan -destroy -var-file="$HOME/.bravo/ci-validation-20261103/operator.tfvars" -out="$HOME/.bravo/ci-validation-20261103/destroy.tfplan"
terraform apply "$HOME/.bravo/ci-validation-20261103/destroy.tfplan"
```

정리 대상은 이 state의 자원만이다. 기존 foundation, 다른 태그의 인스턴스, 과거 kubeconfig는 이 정리 대상이 아니다.

## PR #77 후속: infra Python 소비자 시험 선택

리뷰 기준 `7d154b791f114c64f9ad1354fc568d4aa3f5b44b`에서 `infra/envs/cost-aws/verify.py` 또는 T6 `infra/envs/source-aws/preflight.py`만 변경하면 `infra=true, scripts=false, projects=[]`였다. Terraform/IaC 검사는 이 Python 구현을 실행하지 않으므로, 실제로 import하는 repository unittest가 생략됐다.

`infra/**/*.py`의 변경·삭제에는 `scripts=true`도 설정하도록 보완했다. Terraform만 바뀐 경우의 검사 범위와 필수 job 이름은 유지했다. 새 의존성 목록이나 workflow 예외는 추가하지 않았다.

- 회귀 시험을 먼저 추가했을 때 원본 선택기에서 소비자 생략 2건·삭제 경로 1건이 실패했다. 수정 후 `scripts/tests` **33개 시험 통과**, 수정 Python의 Ruff 0.17.3·actionlint 1.7.7·공백 검사 통과.
- 커밋된 선택기 회귀 시험은 두 경로의 합성 구현과 이를 import하는 소비자를 임시 폴더에 만든다. 선택된 repository unittest 명령이 정상 코드에서는 종료 0, 문법 오류를 넣으면 비정상 종료인지 확인한다.
- 별도 로컬 교차 확인에서는 실제 `verify.py`와 소비자 시험 **3개**, PR #82의 `ea4ec860c9d60ec11ffe551873e351ccda41b488`에 있는 실제 T6 코드와 소비자 시험 **15개**를 각각 임시 사본으로 실행했다. 두 정상 사본 모두 종료 0이었고, 구현에 문법 오류를 넣으면 모두 `SyntaxError`·종료 1이었다. 파일·기준 커밋·선택 결과는 [JSON 기록](infra-python-consumer-regression.json)에 있다.

실제 코드 재현은 기록된 커밋에서 implementation·consumer·supporting_files를 별도 폴더에 복사하고 `python3 -B -m unittest discover -s scripts/tests -q`로 실행했다. 이후 임시 implementation 끝에 `def injected_syntax_error(:`를 추가하고 같은 명령의 실패를 확인했다. T6 사본은 별도 PR의 미병합 코드이며 이번 PR에 구현을 복사하지 않았다. 이 검증은 로컬 코드·소비자 관계 확인이며 새 AWS 실행이나 배포 증거가 아니다.

게시 후 최신 develop `88fee51`과 AGENTS.md의 추가 문단 충돌을 확인했다. CI 안내와 새 리뷰 플레이북 안내를 모두 보존해 기준 브랜치를 반영했다. 그 결과 저장소 시험은 새 리뷰 도구 시험을 포함해 **68개 통과**했으며, 수정 파일 Ruff·actionlint·공백 검사도 다시 통과했다.
