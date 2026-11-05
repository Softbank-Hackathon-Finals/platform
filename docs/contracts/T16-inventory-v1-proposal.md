# [T16] 의존성 · 설정 인벤토리 출력 계약 v1 제안

상태: **로컬 구현 · 시험 제안, 팀 채택 전**. 기준: `docs/design.html` 8.11 등록 모델, FR-1 · FR-2 · FR-16 · FR-17, 7.3 대체 구현 카탈로그. 소비자는 T21 콘솔, T11 에이전트(번역 입력 FR-4), T32 이전 대상 추천이다. 설계 문서 11장이 "11/3 착수 전 고정"으로 지정한 계약이며, 원본 스키마는 [`inventory.schema.json`](../../services/inventory/src/bravo_inventory/schemas/inventory.schema.json)이다.

## 문서 구조

| 필드 | 의미 |
|---|---|
| `schema_version` | `1.0`. 인식하지 못하면 소비자는 적용을 멈춘다 |
| `app` | 등록된 앱: `id`, `repo`(등록된 레포 참조. 로컬 체크아웃 경로를 쓰지 않는다), `app_path`, `source_sha`(신뢰된 수집기가 발급. 이 모듈은 값을 검증하지 않는다) |
| `source` | 출발지 환경 `env_id` · `env_kind`(`aws` · `gcp` · `onprem`) · `namespace` |
| `inputs` | `repo_digest`(스캐너가 읽은 파일 전체: `app_path`의 Python + 레포 전체 설정 파일의 상대 경로 · 바이트 해시), `deploy_digest`(배포 그래프 해시), `api_calls`(배포 추적에 쓴 파일 · API 목록) |
| `items[]` | 의존성 항목. 아래 표 |
| `deploy` | 추적한 배포 그래프: 워크로드(이미지 · 서비스 계정 · 환경 변수 이름), 자원(종류 · 이름 · 경로 `via` · 허용 동작 · 속성), 메모 |
| `candidates[]` | 후보 목적지별 적합성: `verdict`, `grade_counts`, `missing_capabilities`, `residual_dependencies`, `capabilities`(값마다 `declared` 또는 `observed` 출처) |
| `summary` | 등급별 개수, `agent_needed`, `deploy_only`, `code_only` |
| `digest` | `generated_at`을 뺀 문서 전체의 SHA-256. 같은 입력이면 같다 |

## 항목(`items[]`)

| 필드 | 값 | 규칙 |
|---|---|---|
| `role` | `object_store` · `secrets` · `kms` · `iam` · `db` · `audit` · `runtime` · `messaging` · `function` · `ai` · `unknown` … | 7.3 대응표의 역할. SDK 서비스명은 `catalog.SERVICE_ROLE`로 매핑 |
| `found_in` | `code` · `deploy` · `both` | `deploy`면 `risk = migration_omission` 필수(스키마가 강제). 코드가 쓰는 역할이라도 워크로드 설정(env 이름)이 가리키지 않는 버킷 · 시크릿 · 키는 같은 `role`의 별도 `deploy` 항목이 된다 |
| `grade` | `config_swap` · `adapter` · `code_change` · `none` · `unknown` | FR-2 3등급 + 대체 없음 + 미지 |
| `grade_by` | `rule` · `agent` | `agent`면 `rationale` 10자 이상 필수. 규칙이 `none`으로 정한 항목은 에이전트가 바꾸지 못한다 |
| `agent_needed` | bool | 규칙이 못 정했거나(미지 역할, 와일드카드 정책) 배포에만 있는 항목 |
| `code_refs[]` | `id` · `kind` · `file` · `line` · `snippet` · `detail` | `snippet`은 마스킹된 한 줄. 비밀처럼 보이는 값은 `<masked>` |
| `deploy_refs[]` | `kind` · `name` · `via` · `actions` · `attributes` | `via`는 네임스페이스에서 자원까지의 경로 문장 |
| `target` | `env_kind` · `impl` · `verify` | 카탈로그의 목적지 구현과 검증 방법(게이트 질문의 근거) |

## 소비자별 약속

- **T21 콘솔**: `items`를 등급별로 표시하고 `found_in = deploy` 항목을 강조한다. `candidates[].capabilities[*].source`로 등록값과 관측값을 구분해 보여 준다.
- **T11 에이전트**: `agent_needed = true` 항목과 `deploy.resources`(정책 문)를 번역 입력으로 받는다. 에이전트의 출력은 `plan.propose` 제안서이며, 인벤토리 자체를 고쳐 쓰지 않는다. 플랫폼이 제안서를 받아 `grade_by: agent`로 병합하고 스키마를 다시 검증한다.
- **T32 추천**: `candidates[]`의 `verdict`가 `blocked`인 후보는 추천 순위에서 제외한다. AI는 `grade_counts` · `missing_capabilities` · `residual_dependencies`와 운영팀 입력을 읽고 순위와 이유만 쓴다.

## 보장하지 않는 것

- 정적 탐지는 환경 변수로 조립되는 동적 엔드포인트와 리플렉션 호출을 놓칠 수 있다. 리허설 네임스페이스(T3 · T15)의 송신 차단 기록을 2차 증거로 합치는 것은 후속 제안이다.
- `source_sha`와 레포 스냅샷의 결속은 T10 · T15의 신뢰된 수집기 몫이다. 이 문서의 `repo_digest`는 "무엇을 읽었는가"의 기록이지 커밋 증명이 아니다.
- `Boto3Adapter`의 실제 AWS 호출은 2026-11-06 개인 SSO 사전 점검에서만 실측했다([요약](../evidence/t16-real-shakedown-2026-11-06/README.md)). 연결용 공식 수집은 T7 읽기 전용 수집 역할로 다시 만든다. 허용 목록 밖 API는 호출 전에 거부된다.
