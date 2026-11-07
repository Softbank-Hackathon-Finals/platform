# [T16] 의존성 · 설정 인벤토리 출력 계약 v1

상태: **v1 고정**(`schema_version: "1.0"`). 기준: `docs/design.html` 8.11 등록 모델, FR-1 · FR-2 · FR-16 · FR-17, 7.3 대체 구현 카탈로그, [ADR-0018](../adr/0018-dependency-grading-rules.md). 설계 문서 11장이 "11/3 착수 전 고정"으로 지정한 계약이다. 원본 스키마는 [`inventory.schema.json`](../../services/inventory/src/bravo_inventory/schemas/inventory.schema.json)이고, 이 문서와 스키마가 다르면 스키마가 맞다.

## 생산자와 소비자

| 쪽 | 위치 | 읽는 방법 |
|---|---|---|
| 생산 | `services/inventory` `report.build_inventory` · `bravo-inventory scan` | 내보내기 전에 스키마로 검증한다 |
| T21 콘솔 | `services/console` `workspace_reports.project_inventory` | 생산자 스키마(`validate_inventory`)와 digest 재계산 |
| T10 최초 제안 | `services/platform-events` `initial_source.inventory_analysis` | 생산자 스키마와 digest 재계산, `source_sha` 대조 |
| T11 에이전트 | `services/agent-worker` `InventorySummary.from_document` | digest 재계산 후 `items` 일부 필드만 모델에 보인다 |
| T32 추천 | `services/target-recommend` `recommend` | `schema_version == "1.0"`, `candidates`만 읽는다 |
| T27 출구 훈련 | `services/exit-drill` `inventory_input.parse_inventory` | `schema_version == "1.0"`, digest 재계산, 후보 하나의 `items` · `candidates` · `deploy.notes` |

저장된 문서 사본으로 시험하는 소비자(agent-worker · exit-drill · target-recommend의 fixture)는 `services/inventory/tests/test_contract.py`가 생산자 스키마와 digest로 다시 검증한다. 생산자 형식이 바뀌어 사본이 어긋나면 inventory 시험이 먼저 실패한다.

## 문서 구조

| 필드 | 의미 |
|---|---|
| `schema_version` | `"1.0"`. 인식하지 못하면 소비자는 적용을 멈춘다 |
| `app` | 등록된 앱: `id`, `repo`(등록된 레포 참조. 로컬 체크아웃 경로를 쓰지 않는다), `app_path`, `source_sha`(신뢰된 수집기가 발급. 이 모듈은 값을 검증하지 않는다) |
| `source` | 출발지 환경 `env_id` · `env_kind`(`aws` · `gcp` · `onprem`) · `namespace` |
| `generated_at` | 생성 시각(UTC). digest에 들어가지 않는다 |
| `inputs` | `repo_digest`(스캐너가 읽은 파일 전체: `app_path`의 Python + 레포 전체 설정 파일의 상대 경로 · 바이트 해시), `deploy_digest`(배포 그래프 해시), `api_calls`(배포 추적에 쓴 파일 · API 목록) |
| `items[]` | 의존성 항목. 아래 표 |
| `deploy` | 추적한 배포 그래프: `namespace`, 워크로드(이미지 · 서비스 계정 · 환경 변수 이름), 자원(종류 · 이름 · 경로 `via` · 허용 동작 · 속성), `api_calls`, `notes`(문자열 목록. 못 읽은 입력이 여기 적힌다) |
| `candidates[]` | 후보 목적지별 적합성. 아래 표 |
| `summary` | 등급별 개수, `agent_needed`, `deploy_only`, `code_only` |
| `digest` | `generated_at`과 `digest`를 뺀 문서 전체를 키 정렬 · 공백 없는 JSON(`ensure_ascii=False`)으로 만든 SHA-256, `sha256:<64 hex>`. 같은 입력이면 같다 |

## 항목(`items[]`)

| 필드 | 값 | 규칙 |
|---|---|---|
| `id` | `item-NNN` | 문서 안에서 유일 |
| `role` | `object_store` · `secrets` · `kms` · `iam` · `db` · `audit` · `runtime` · `messaging` · `function` · `ai` · `unknown` … | 7.3 대응표의 역할. SDK 서비스명은 `catalog.SERVICE_ROLE`로 매핑 |
| `service` | 문자열 | 코드에서 찾은 SDK 이름을 `", "`로 잇는다. 없으면 역할 이름 |
| `found_in` | `code` · `deploy` · `both` | `deploy`면 `risk = migration_omission` 필수(스키마가 강제). 코드가 쓰는 역할이라도 워크로드 설정(env 이름)이 가리키지 않는 버킷 · 시크릿 · 키는 같은 `role`의 별도 `deploy` 항목이 된다 |
| `grade` | `config_swap` · `adapter` · `code_change` · `none` · `unknown` | FR-2 3등급 + 대체 없음 + 미지 |
| `grade_by` | `rule` · `agent` | `agent`면 `rationale` 10자 이상 필수. 규칙이 `none`으로 정한 항목은 에이전트가 바꾸지 못한다 |
| `rationale` | 1자 이상 | 등급 근거 |
| `agent_needed` | bool | 규칙이 못 정했거나(미지 역할, 와일드카드 정책) 배포에만 있는 항목 |
| `code_refs[]` | `id` · `kind` · `file` · `line` · `snippet` · `detail` | `snippet`은 마스킹된 한 줄. 비밀처럼 보이는 값은 `<masked>` |
| `deploy_refs[]` | `kind` · `name` · `via` · `actions` · `attributes` | `via`는 네임스페이스에서 자원까지의 경로 문장 |
| `target` | `env_kind` · `impl` · `verify` | 첫 후보 종류 기준의 목적지 구현과 검증 방법(게이트 질문의 근거). 다른 종류의 후보에는 쓰지 않는다 |
| `risk` | `""` · `migration_omission` | |

## 후보(`candidates[]`)와 제공 기능

| 필드 | 규칙 |
|---|---|
| `env_id` · `env_kind` | 등록 프로필의 환경 |
| `verdict` | `fits` · `needs_work` · `blocked`. 규칙만 정한다. `none` 항목이 있거나 필요한 기능이 제공되지 않으면 `blocked` |
| `grade_counts` | 등급별 개수 |
| `missing_capabilities` | 역할마다 한 문장. 기능 키를 작은따옴표로 한 번 인용한다(`… 's3_compatible' 기능이 등록되지 않음` 또는 `… 관측되지 않음`). T32가 이 인용으로 키를 뽑으므로 문장 형식을 바꾸면 새 버전이다 |
| `residual_dependencies` | 이전 뒤에도 남는 외부 의존성(문자열) |
| `capabilities` | `{기능 키: {value, source}}`. 아래 규칙 |

`capabilities[*].source`는 `declared: …`(운영자가 등록한 프로필), `observed: …`(읽기 전용으로 본 것), `unknown: …`(감지 불가) 중 하나로 시작한다. `value`가 `unknown`이면 출처도 `unknown:`이어야 한다(스키마가 강제). `value: "absent"`는 완전한 답(서빙 중인 API 그룹 목록, RKE2 노드 인자)에서 없음을 관측한 것이다. `unknown`과 `absent`는 적합성에서 "제공"으로 세지 않는다. 관측이 `unknown`이면 등록값을 덮지 않고, `absent`는 등록값을 덮는다.

### 환경 제공 기능 감지(등록 시)

`bravo_inventory.k8s_capabilities.detect_capabilities(get)`이 다섯 기능을 항상 내보낸다. `get(path, params)`는 호출자가 주는 HTTP GET 하나이며, 모듈은 요청 본문을 만들지 않고 `READ_PATHS` 밖의 경로는 호출 전에 거부한다.

| 키 | 읽는 곳 | 값 |
|---|---|---|
| `k8s_version` | `GET /version`의 `gitVersion` | 예: `v1.31.4+rke2r1`. 못 읽으면 `unknown` |
| `postgres_operator` | `GET /apis`의 서빙 그룹(`postgresql.cnpg.io` · `acid.zalan.do` · `postgres-operator.crunchydata.com`) | `cnpg` 등. 목록을 읽었는데 없으면 `absent` |
| `s3_compatible` | `/apis`의 `seaweed.seaweedfs.com` · `minio.min.io` · `objectbucket.io`, 전체 Service 목록의 `seaweedfs` · `minio` 이름 | 찾으면 값, 못 찾으면 `unknown`(클러스터 밖 저장소일 수 있어 `absent`로 쓰지 않는다) |
| `secret_store` | Service 목록의 `openbao` · `vault`, `/apis`의 `secrets.hashicorp.com`, ESO `clustersecretstores` · `secretstores`의 `vault` 공급자 | `openbao` · `vault` · `vault-api`, 못 찾으면 `unknown` |
| `cis_profile` | 전체 Node의 `rke2.io/node-args` 주석에서 `--profile` | 모든 노드가 같으면 `rke2-<profile>`, 모두 없으면 `absent`, 주석 누락 · 불일치 · 목록 절단은 `unknown` |

ESO 그룹이 서빙되면 `secrets_injection: eso`도 더한다. `/version`과 `/apis`는 기본 RBAC에서 인증된 주체 모두가 읽는다. Node · 전체 Service · SecretStore 목록은 추가 읽기 권한이 필요하며, 없으면 `unknown`으로 남는다(추측하지 않는다). 파일로 돌릴 때는 목적지 내보내기 디렉터리에 `kubectl get --raw` 결과를 `version.json` · `apis.json` · `nodes.json` · `services.json` · `clustersecretstores.json` · `secretstores.json`으로 둔다. 이 파일들이 없으면 기존 출력과 같다.

T10 등록 probe(`registration_probe.KubernetesProbe`)에는 아직 연결하지 않았다. 연결은 probe의 GET 요청 함수를 넘겨 결과를 등록 영수증 또는 T16 수집 입력에 싣는 방식이며, T10 영수증 형식 변경이 따르므로 T10 담당과 합의해 다음 단계로 한다.

## 버전과 호환 규칙

- v1 문서는 `schema_version: "1.0"`을 유지한다. 소비자는 이 값을 확인하고, 모르는 값이면 적용을 멈춘다(T32 · T27은 `"1.0"`만, agent-worker는 `1.x`를 받는다).
- **호환 변경(같은 `"1.0"`)**: 선택 필드 추가, `capabilities`의 새 기능 키, `deploy`의 새 메모. 생산자는 스키마에 먼저 추가하고, 소비자는 모르는 필드를 무시한다. 필수 필드는 추가하지 않는다.
- **새 버전(`"2.0"`과 새 계약 문서)**: 필드 삭제 · 이름 변경, 선택 필드를 필수로 바꾸기, 값의 의미 변경(등급 · `found_in` · `verdict` · `risk` 열거값의 뜻, `missing_capabilities` 문장 형식, `capabilities` 출처 접두어), digest 계산 방식 변경. 새 버전을 내는 PR은 소비자별 이전 순서를 함께 적는다.
- 스키마를 바꾸는 PR은 위 소비자 표의 담당자를 리뷰에 넣고, 저장된 사본이 있으면 `test_contract.py`로 함께 갱신한다.

## 소비자별 약속

- **T21 콘솔**: `items`를 등급별로 표시하고 `found_in = deploy` 항목을 강조한다. 후보는 `verdict` · `missing_capabilities` · `residual_dependencies`를 보여 준다. `candidates[].capabilities[*].source`로 등록값 · 관측값 · 감지 불가를 구분해 보여 주는 것은 계약이 허용하는 표시이며, 현재 콘솔은 아직 표시하지 않는다(T21 후속).
- **T11 에이전트**: `agent_needed = true` 항목과 `deploy.resources`(정책 문)를 번역 입력으로 받는다. 에이전트의 출력은 `plan.propose` 제안서이며, 인벤토리 자체를 고쳐 쓰지 않는다. 플랫폼이 제안서를 받아 `grade_by: agent`로 병합하고 스키마를 다시 검증한다.
- **T32 추천**: `candidates[]`의 `verdict`가 `blocked`인 후보는 추천 순위에서 제외한다. AI는 `grade_counts` · `missing_capabilities` · `residual_dependencies`와 운영팀 입력을 읽고 순위와 이유만 쓴다.

## 보장하지 않는 것

- 정적 탐지는 환경 변수로 조립되는 동적 엔드포인트와 리플렉션 호출을 놓칠 수 있다. 리허설 네임스페이스(T3 · T15)의 송신 차단 기록을 2차 증거로 합치는 것은 후속 제안이다.
- `source_sha`와 레포 스냅샷의 결속은 T10 · T15의 신뢰된 수집기 몫이다. 이 문서의 `repo_digest`는 "무엇을 읽었는가"의 기록이지 커밋 증명이 아니다.
- 제공 기능 감지는 관측 시점의 값이다. 클러스터가 바뀔 수 있으므로 실행 단계는 각자 다시 확인한다. 실제 클러스터 감지는 아직 실측하지 않았다(가짜 응답 시험만).
- `Boto3Adapter`의 실제 AWS 호출은 2026-11-06 개인 SSO 사전 점검에서만 실측했다([요약](../evidence/t16-real-shakedown-2026-11-06/README.md)). 연결용 공식 수집은 T7 읽기 전용 수집 역할로 다시 만든다. 허용 목록 밖 API는 호출 전에 거부된다.
