# T8 첨부 복사 이미지 빌드 · 전달 실측 (2026-11-06)

운영자 머신에서 검토된 커밋으로 amd64 이미지를 만들어, 게시 이미지와 같은 검사를 통과시키고, amd64 노드의 containerd에 digest 이름으로 넣는 데까지 재었다. **실제 VM으로 옮기지 않았고, amd64 이미지로 Job을 돌리지도 못했다.** 클라우드 자원은 호출하지 않았다. 레지스트리, 새 키, 새 계정, 새 워크플로도 쓰지 않았다.

숫자는 모두 스크립트가 실행 결과에서 만든 [image-check.json](image-check.json), [k3s-result.json](k3s-result.json), [mutations.json](mutations.json)에서 왔다. 잰 코드는 각 파일의 `source_revision`이다. 절차는 [운영 안내](../../operations/t8-attachment-copy-job.md#이미지-빌드와-전달)에 있다.

## 왜 새 스크립트인가

| 기존 경로 | 재사용 | 이유 |
| --- | --- | --- |
| `scripts/export_target_image.py` | 안 함 | demo-chat의 소스 · digest · blob 수가 코드에 고정돼 있다 |
| `publish-executor-image.yml` | 안 함 | Executor 전용이고 T7 소유라 고치지 않는다. ECR 저장소와 게시 역할이 새로 필요하다 |
| `scripts/executor_image.py`의 `verify_layout`, `scan_summary` | **그대로 호출** | 게시 이미지의 합격 기준이므로 복제하지 않았다 |
| `docs/operations/target-image-handoff.md`의 SSH · `ctr` 규칙 | **따름** | 해시 대조 · 가져오기 · 이름 추가 규칙이 같다 |

저장소의 이미지 경로 중 서명을 쓰는 곳은 없다. 그래서 기준은 provenance + SBOM + Trivy + blob 검증이고, 이 경로도 같다. 다른 점은 CI 러너가 아니라 운영자 머신에서 빌드한다는 것이며, 영수증에 `built_in_ci: false`로 남는다.

## 잰 것

### 실제 amd64 빌드 (점검 9개 통과, 77초)

| 항목 | 결과 |
| --- | --- |
| 빌드 | `linux/amd64` OCI index + provenance + SBOM, blob 15개, 묶음 73,308,160바이트 |
| Trivy 0.70.0 (`vuln,secret`, HIGH · CRITICAL, 수정 가능한 것) | Debian · Python 패키지 검사, 탐지 0건 |
| `executor_image.verify_layout` | 빌드 직후와 묶음 안에서 모두 통과 |
| 격리 실행 검사 (네트워크 없음, 읽기 전용, capability 없음) | boto3 · psycopg · 복사 모듈 import, UID 10001, pip 없음 |
| 이전 베이스 digest | `image_scan_failed`, 묶음 없음 |

### amd64 노드에 가져오기 (에뮬레이션 k3s의 containerd)

| 항목 | 결과 |
| --- | --- |
| 노드에서 계산한 묶음 해시 | 영수증과 같음 |
| 안내의 `ctr images import` | `sha-<커밋>` 이름만 생기고 digest 이름은 생기지 않음 |
| 안내의 이름 추가 | digest 이름이 영수증 digest를 가리킴, 플랫폼 `linux/amd64` |
| 안내의 `grep` 필터 (실제 `ctr` 출력) | 태그 · digest 이름은 찾고, 다른 digest는 거부 |

### Job 실행 (임시 k3s, 점검 26개 통과, 285초)

운영자 스크립트와 같은 빌드 명령 · 묶음 · `ctr` 인수로 만든 이미지로 Job을 돌렸다. 플랫폼만 이 머신의 arm64다. 첫 복사 3개, 재실행 `reused` 3개, 실패 · 미확인 판정이 [이전 측정](../t8-attachment-job-2026-11-06/README.md)과 같게 나왔다. 런타임이 보고한 `imageID`도 OCI index digest와 맞았다. 대역 구성은 이전 측정과 같다.

### 일부러 검사를 뺀 결과

167개를 시험 132개로 돌려 모두 잡았다. 그중 40개가 이번 스크립트다. 첫 실행에서 3개가 살아남았다. 1개는 묶음 검증을 재는 시험이 없어 추가했고, 2개는 다른 검사가 이미 맡고 있는 중복 코드라 지웠다.

## 발견한 것

- **베이스 이미지의 HIGH 취약점:** 이전 digest는 `apt` 2.6.1에 `CVE-2026-109518`(수정판 2.6.2)이 있어 스캔에서 걸렸다. 예외를 두지 않고 현재 `python:3.12-slim-bookworm` digest로 올렸다. `services/cutover-executor`와 `services/rehearsal-runner`의 credential probe도 이전 digest를 쓴다. 그 이미지들은 재지 않았지만 같은 설정이면 다음 스캔에서 걸릴 가능성이 높다.
- **이름 없는 이미지의 첨부 증명:** BuildKit은 이미지 이름이 없으면 provenance · SBOM의 `subject`를 비워 두고, `verify_layout`은 이를 거부한다. CI의 `tags:`처럼 이름을 주어 해결했다. 검증기는 고치지 않았다.
- **arm64 노드와 amd64 전용 index:** containerd가 플랫폼이 맞지 않는다며 이미지를 없는 것으로 본다. 같은 이유로 amd64 바이트를 이 Mac의 arm64 k3s에서 돌릴 수 없다.

## 재지 않은 것

- **amd64 바이트로 돌린 Job:** 에뮬레이션 amd64 노드는 seccomp를 지원하지 않아 Pod가 뜨지 않고, `restricted` Job에는 seccomp가 필수다. amd64 바이트는 격리 실행 검사와 가져오기까지, Job 전체는 arm64 이미지로만 쟀다.
- **실제 VM:** SSH 전달, RKE2의 `ctr` · 소켓, RKE2의 containerd 버전이 같은 이름을 만드는지, RKE2가 보고하는 `imageID`.
- **다른 운영자 머신:** 이 머신의 Docker 29.6과 containerd 이미지 저장소에서만 쟀다.
- **실제 첨부 복사:** S3 · KMS · Tailscale 경로는 이번 범위가 아니며 [이전 측정의 미확인 항목](../t8-attachment-job-2026-11-06/README.md#재지-않은-것)이 그대로 남아 있다.
