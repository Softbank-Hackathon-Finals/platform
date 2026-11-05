# 의존성 · 설정 인벤토리 (T16)

등록된 앱의 **코드 · IaC**와 **실제 배포**에서 클라우드 의존성을 찾아 3등급으로 나누고, 레포와 배포의 차이를 표시한다(설계 8.11, FR-1 · FR-2 · FR-16). 출력은 [`inventory.schema.json`](src/bravo_inventory/schemas/inventory.schema.json)으로 검증하며 T21 콘솔 · T11 에이전트(번역 입력, FR-4) · T32 추천이 읽는다. 계약 설명은 [`docs/contracts/T16-inventory-v1-proposal.md`](../../docs/contracts/T16-inventory-v1-proposal.md).

**결정적이다.** 이 패키지는 LLM을 import하지 않는다. 규칙이 정하지 못한 항목은 `agent_needed: true`로 표시만 하고, 에이전트(T11)가 분류한 결과는 `grade_by: "agent"`와 이유를 붙여 같은 스키마로 다시 검증한다. 규칙이 `none`(대체 없음)으로 판정한 항목은 에이전트가 뒤집지 못한다.

## 구성

| 모듈 | 책임 |
|---|---|
| `static_scan.py` | Python `ast`로 `boto3.client("…")` 호출 · SDK import · 환경 변수 참조 · 리전 · 엔드포인트 · ARN 리터럴 탐지. compose · Kubernetes YAML · `.env.example` · Terraform의 환경 변수 · `aws_*` 자원 탐지. 비밀처럼 보이는 값은 마스킹 |
| `deploy_trace.py` | 네임스페이스 → Deployment → ServiceAccount → IRSA 역할 → 정책 → S3 버킷 · Secrets Manager 항목 · KMS 키, CNPG 클러스터 추적. `FileAdapter`(`kubectl get -o json` 내보내기 + `aws.json`)와 `Boto3Adapter`(읽기 전용 API 허용 목록, 호출 기록) |
| `catalog.py` | 7.3 대체 구현 카탈로그(역할 → 목적지 구현 · 기본 등급 · 검증 방법). 데이터이며 로직이 아니다 |
| `grading.py` | 3등급 규칙. `config_swap` / `adapter` / `code_change` / `none` / `unknown` |
| `reconcile.py` | 코드 ↔ 배포 대조: `both` · `code`(코드에만) · `deploy`(배포에만 → `migration_omission` 위험 표시) |
| `capabilities.py` | 목적지 제공 기능: 프로필(등록값) + 내보내기에서 관측(CRD · 알려진 워크로드). 후보별 적합성(`fits` / `needs_work` / `blocked`) |
| `report.py` · `cli.py` | 문서 조립 · 입력 digest · 스키마 검증 · 한 번만 쓰는 CLI |

## 실행

```sh
cd services/inventory
uv sync --locked && uvx ruff==0.17.3 check . && uv run --locked pytest -q

uv run bravo-inventory scan \
  --app-id demo-chat --repo ../../../demo-chat --app-path app \
  --source-env aws-seoul --namespace chat \
  --exports /private/path/exports/aws \
  --profile profiles/onprem-rke2.json --profile profiles/onprem-minimal.json \
  --target-exports onprem-demo=/private/path/exports/onprem \
  --output /private/path/inventory.json
```

내보내기 디렉터리에는 `deployments.json` · `serviceaccounts.json` · `clusters.json`(`kubectl get … -o json`)과 `aws.json`(역할 → 정책 문, 버킷 · 시크릿 · 키 속성)을 둔다. `--aws-live`를 주면 `aws` extra의 boto3로 IAM 정책과 버킷 공개 여부를 실제로 읽는다. 허용 목록(`READ_ONLY_API`) 밖의 호출은 어댑터가 호출 전에 거부하고, 사용한 API 목록은 출력의 `inputs.api_calls`에 남는다. kubeconfig는 이 모듈에 주지 않는다(내보내기는 T7 수집 역할이 만든다).

## 3등급 규칙 (요약)

| 역할 | 코드 증거 | 등급 | 이유 |
|---|---|---|---|
| object_store | S3 SDK 호출 **전부** `endpoint_url`이 설정에서 옴 | `config_swap` | S3 호환 저장소로 주소만 교체 |
| object_store | S3 SDK 호출 중 하나라도 endpoint 설정 없음(없거나 리터럴) | `code_change` | 호출 단위로 집계. 일부만 설정 가능하면 `agent_needed`(같은 설정을 공유하는지 규칙으로 확정 불가) |
| secrets · kms | SDK 직접 호출 | `code_change` | OpenBao · ESO 주입 방식으로 코드 수정 (설계 8.4 P0 작업) |
| iam | 역할 · 정책 | `adapter` | RBAC · 저장소 정책으로 번역. 와일드카드 자원이면 `agent_needed` |
| db | 접속 설정 | `config_swap` | 같은 엔진(CNPG), 데이터는 복제로 이전 |
| 배포에만 있음 | 코드에 호출 없음 | 카탈로그 기본 등급 + `agent_needed` | 암묵적 사용(SSE-KMS 등) 여부를 에이전트가 확인 |
| 카탈로그 밖 | SQS · Lambda · 타 클라우드 SDK | `none` 또는 `unknown` | 대체 없음은 규칙이 확정, 미지는 에이전트 분류 |

## 현재 범위와 남은 것

- **실물**: Python 정적 탐지, 설정 파일 탐지, 파일 기반 배포 추적, 규칙 등급, 레포 · 배포 대조, 제공 기능 관측, 스키마 검증, CLI. demo-chat 사본과 합성 내보내기로 19개 시험.
- **미검증**: `Boto3Adapter`의 실제 AWS 호출(T6 출발지와 T7 수집 역할이 준비되면 실측). GCP 어댑터 없음(P2).
- **미구현**: 에이전트 분류 결과를 받아 `grade_by: "agent"`로 병합하는 입력 경로(T11 연결), `source_sha`를 신뢰된 수집기가 발급하는 결속(T10). 비용 추정(T32 P2).
- 이 모듈은 신뢰된 서버 내부 라이브러리다. 레포 경로 · 내보내기 · 프로필은 호출 서버가 등록 정보로 고정하며, 요청자가 임의 경로를 넘기지 않게 한다.
