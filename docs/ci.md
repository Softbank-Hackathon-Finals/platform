# 저장소 CI와 플랫폼의 실패 분석

기준: `docs/design.html` 8.3·8.4, T7·T11·T15·T31. 저장소 품질 검사와 플랫폼 S2의 모델 제안은 별개 실행 경계다. 모델은 CI 합격·배포·병합 권한을 갖지 않는다.

## 실행 범위

`scripts/ci_scope.py`는 Git의 정확한 base SHA와 merge-base, NUL 구분 diff를 읽는다. 이동 전후 경로를 모두 검사한다. 프로젝트는 tracked `pyproject.toml`·`package.json`·`go.mod`로 발견하며 별도 서비스 목록을 관리하지 않는다.

| 변경 | 실행 |
|---|---|
| 문서·README만 | scope 회귀 시험, 전체 이력 secrets, 필수 infra/platform의 명시적 결과 |
| 한 서비스의 코드·lock·테스트 | 해당 프로젝트 검사와 의존성 취약점 검사 |
| 서비스의 Kubernetes YAML·Dockerfile | 해당 프로젝트 + IaC·Dockerfile 검사 |
| infra | Terraform root/module 전체 fmt·validate·mock tests와 IaC 검사 |
| infra 아래 Python 코드·삭제 | 위 infra 검사 + 해당 코드를 import하는 repository unittest |
| scripts 일반 도구 | repository unittest |
| workflow·CI 선택기·공통 계약·루트 설정 | 전체 검사 |
| 알 수 없는 경로·비교 이력 없음·삭제된 manifest | 전체 검사 |
| local path/workspace dependency가 있는 프로젝트 변경 | 소비자 누락을 피하기 위해 전체 검사 |
| main/develop push·주간 예약·수동 실행 | 전체 검사 |

Terraform은 아직 root 간 의존 그래프를 만들지 않는다. 공통 module 소비자를 빠뜨리지 않도록 infra 변경 시 모두 검증한다. 현재 규모에서는 이 보수적 범위가 수작업 의존 목록보다 관리하기 쉽다. 프로젝트가 많아지면 실제 실행 시간을 보고 분할한다.

`infra/**/*.py`는 Terraform/IaC 검사만으로 실행 오류를 확인할 수 없다. `cost-aws/verify.py`나 T6 `source-aws/preflight.py`를 가져다 쓰는 `scripts/tests`가 있으므로 Python 변경·삭제에는 repository unittest도 보수적으로 선택한다. 소비자 목록을 따로 등록하지 않는다. 선택기 회귀 시험은 두 경로에서 정상 fixture가 통과하고 문법 오류를 넣은 임시 구현이 실제 unittest 명령을 실패시키는지 확인한다.

필수 `secrets`, `infra`, `platform` 이름은 유지한다. 워크플로 자체에는 path filter를 두지 않는다. `platform`은 `always()`로 scope·선택 프로젝트의 결과를 확인한다. 선택된 검사가 skipped/cancelled/failure이면 실패하며, 선택이 빈 경우에만 skipped를 허용한다. scope 자체 실패도 통과시키지 않는다. [GitHub 필수 검사 규칙](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)

프로젝트 검사는 최대 3개 병렬, 각 20분이다. 같은 PR의 이전 실행은 새 커밋이 오면 취소한다. develop/main 통합 검사는 취소하지 않는다. 전체 실행에서는 저장소 전체 취약점 검사를 한 번만 하고, 부분 실행에서는 선택 프로젝트만 검사한다. Python은 `uv sync --locked`로 manifest와 lock의 일치까지 확인하고 lock에 든 pytest, 고정 Ruff 0.17.3을 쓴다.

```sh
python3 scripts/ci_scope.py --base <40자리-base-SHA>
python3 scripts/ci_scope.py --full
python3 scripts/ci_project.py services/rehearsal-runner python
python3 -m unittest discover -s scripts/tests -v
trivy config --ignorefile .trivyignore.yaml --severity HIGH,CRITICAL --exit-code 1 .
```

## 보안·권한·절차

PR 검사는 `contents: read`만 쓴다. 클라우드 배포/모델 키를 PR에 주입하지 않는다. 검사 선택과 판정에 LLM을 넣지 않는다. 새 AI 수정 기능이 workflow·검증 정책·lock을 고쳐 검사를 무력화하지 않도록, 모델의 쓰기 허용 경로는 별도 제안 계약에서 제한한다.

Trivy 예외는 `.trivyignore.yaml`의 정확한 파일 경로에 한정한다. 기존 리허설 Role의 KSV-0053/0056 예외도 저장소 전역에서 해당 Role 파일로 좁혔다. 임시 AWS 검증 호스트의 외부 HTTPS(AWS-0104)는 bootstrap·SSM을 위한 예외이며 2026-11-10 만료다. 이 호스트는 인바운드 0·90분 종료·운영 네트워크 미연결이며, Pod는 별도 default-deny 시험을 통과해야 한다. [Trivy 경로·만료일 필터](https://trivy.dev/docs/latest/configuration/filtering/)

보드 동기화는 CI와 다른 workflow다. 상태가 같으면 쓰지 않으며, 이슈 체크와 PR 승인을 혼동하지 않는다. 이벤트 병합으로 누락된 상태는 주기적 대조로 복구하고 개별 API·job 시간을 제한한다. GitHub 기본 auto-merge는 최종 필수 CI와 리뷰 승인 1건 이상, 변경 요청 해소를 요구한다. 리뷰 요청 대상 전체의 승인을 기다리는 규칙은 아니다.

## 플랫폼 CI와 S2의 접점

1. T7 신뢰 CI가 고정 revision의 앱 이미지를 빌드하고 GHCR digest를 기록한다. 이 문서의 저장소 검사만으로 해당 배포 작업이 완료됐다고 하지 않는다.
2. 실패한 작업의 허용 파일·정제된 실패 요약만 T11 워커에 제공한다. 성공 경로에는 모델 호출이 없다.
3. 워커는 두 가지 좁은 기능(허용 원본 읽기·구조화 제안 제출)으로 시작한다. 운영 명령·merge·push·배포는 제공하지 않는다.
4. T15는 승인된 정책에 맞는 제안만 격리 리허설에서 적용·검증한다. 비공개 검증기와 테스트는 모델에게 제공하지 않는다. 불명 결과는 NEEDS_HUMAN, 확정 실패의 재시도는 최대 3회다.
5. 검증 증거의 run/revision/proposal digest가 일치해야 승인 후보가 된다. T31 반영은 기존 승인 계약을 따른다.

별도 감독 에이전트, 새 큐·DB, 범용 pipeline DSL은 추가하지 않는다. S2의 책임을 서비스 경계로 나누는 여부는 실제 격리·수명·배포 책임이 달라지는지로 판단한다. 회의에서 에이전트 세분화는 열어 두었으며, 현재 설계의 ‘별도 CI 에이전트 없음’과 회의의 미결 사항을 함께 검토한다.

## 검증 범위

로컬에서 현재 두 서비스의 시험 19개·45개가 통과했다. 선택기 회귀에는 문서·단일 서비스·Kubernetes 정책·공통 계약·삭제·이동·이력 누락·로컬 의존성을 포함한다. 실제 YAML 집계 shell을 32개 상태 조합으로 실행해 선택된 검사의 실패·취소·누락을 거부했다.

[AWS 검증 기록](evidence/ci-validation-2026-11-03/README.md)은 별도 임시 k3s의 실제 격리 시험이다. 운영 RKE2, T7 OIDC 배포, T10 승인, T31 이미지 반영, 실제 모델 호출은 각각 별도 증거가 필요하다. 경로 선택에 따른 비용 감소는 선택된 검사 수로 확인했으며, 아직 병합 후 장기간 실행 시간·청구 절감액을 측정한 결과는 아니다.
