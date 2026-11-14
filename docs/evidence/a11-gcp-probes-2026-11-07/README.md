# A11 GCP 통제 시험 · 출발지 추적 실측 (2026-11-07)

[ADR-0034](../../adr/0034-environment-kind-adapters.md)의 gcp 칸을 실제 GKE 클러스터와 결제가 켜진 GCP 프로젝트에서 실행했다.
A7(#661)의 GCP Probe와 A3(#663)의 gcloud 추적 어댑터 위에 쌓았다. 프로젝트 ID · 번호 · 계정 이메일 · 서비스 계정 주소 · 버킷 접미사는 가렸다
(`GCP_PROJECT_001`, `GCP_PROJECT_NUMBER_001`, `person-001@example.invalid`, `SUFFIX`). 원본 실행 결과는 저장소 밖 비공개 경로(0600)에 있다.

| 파일 | 내용 |
|---|---|
| `control-run.json` | 실측 ControlRun(T5 스키마 검증 통과) · ISO-01.1 객체 다리 · SEC-01.3 유출 모의 결과 |
| `trace-a11-probe.json` | A3 추적 결과(수정 후) |
| `a11-probe.yaml` | 실측용 네임스페이스 매니페스트(식별자 가림) |
| `source-manifest.json` | 측정한 소스의 커밋과 파일 해시(`8674c60c`). 뒤 커밋은 시험 fixture 주소를 `.invalid`로 바꾸고 `gcloud_token`의 가장 대상 주소 검사만 넓혔다(실측 경로 미사용) |

## 실행 방법

- 대상: GKE 클러스터 1개(Workload Identity `GKE_METADATA`, 비공개 노드 + Cloud NAT, 컨트롤 플레인 · 워크로드 로그 Cloud Logging 수집).
- 호출 경로: 새 어댑터 `services/control-runner/src/bravo_controls/gcp_rest.py`. 앱 · 침입자 · 읽기 주체는 각자 파드 안에서 요청을 실행한다
  (`PodExecTransport`). Workload Identity 토큰은 파드를 떠나지 않고, 파드 안 프로그램이 Secret `payload`와 오류 메시지를 지운 뒤 상태 코드만 돌려준다.
  익명 IAM-01.1 클라이언트는 인증 헤더 없는 `UrllibTransport(None)`이다. 서비스 계정 키는 만들지 않았다.
- 실행 도구: `services/control-runner/qualification/gcp_live_probes.py`. 비공개 설정 파일(프로젝트 · 버킷 · generation 등)을 받아
  `default_registry()`로 gcp 수집기를 고르고 `full` 프로필 ControlRun을 만든다.

```sh
cd services/control-runner
python qualification/gcp_live_probes.py --config "$PRIVATE/live-config.json" --out "$PRIVATE/runs"
python qualification/gcp_live_probes.py --config "$PRIVATE/live-config.json" --out "$PRIVATE/runs" --simulate-leak
# A3 추적: a11-probe의 Deployment · ServiceAccount를 kubectl로 내보낸 뒤
cd ../inventory
uv run python -m bravo_inventory.cli trace --source-kind gcp --namespace a11-probe --exports "$PRIVATE/exports" --live --output "$PRIVATE/graph.json"
```

### 실측 자원 (모두 `component=a11`)

| 자원 | 용도 |
|---|---|
| GCS 버킷 `bravo-a11-ctl-attach-SUFFIX` | 앱 첨부 버킷(비공개, 균일 접근, 공개 접근 방지). 시험 객체 1개 |
| GCS 버킷 `bravo-a11-ctl-other-SUFFIX` | ISO-01.1의 "다른 앱" 버킷. 객체 다리는 두 앱의 자기 버킷 읽기가 대조군이라 1개 더 필요했다 |
| Secret `bravo-a11-ctl-db` | 버전 1, 사용자 관리 복제(asia-northeast3), CMEK. 값은 생성 즉시 파이프로 넣고 출력하지 않았다 |
| KMS 키링 `bravo-a11-ctl` · 키 `bravo-a11-ctl-secrets` | Secret CMEK. Secret Manager 서비스 에이전트에 암복호 권한 |
| 서비스 계정 `bravo-a11-app` | 앱 GSA. 첨부 버킷 `objectViewer`, Secret `secretAccessor` |
| 서비스 계정 `bravo-a11-intruder` | 침입자(다른 신원). 다른 앱 버킷 `objectViewer`만 |
| 네임스페이스 `a11-probe` | KSA `a11-app`(→ 앱 GSA) · `a11-intruder`(→ 침입자 GSA) · `a11-reader`(주석 없음, 직접 WI principal). 각 KSA의 `python:3.12-slim` Deployment |
| 읽기 주체(직접 WI principal) | Secret `secretmanager.viewer`, 키 `cloudkms.viewer`, 프로젝트 `logging.viewer`. Secret 값 접근 권한 없음 |
| Cloud Asset API 사용 설정 | A3 추적기의 `asset search-all-iam-policies`에 필요 |

서비스 계정과 키링은 라벨을 지원하지 않아 서비스 계정 설명에 `component=a11`을 적었다.

## Probe별 결과

ControlRun `a11-20261107T115911Z`(커밋 `8674c60c`). 결과의 근거는 상태 코드와 판정 필드뿐이며 객체 내용 · Secret 값은 읽지 않았다.

| 시험 | 판정 | 관측 · 대조군 | 근거 |
|---|---|---|---|
| IAM-01.1 GCS 익명 읽기 | **PASS** | 앱 200 → 익명 **401 UNAUTHENTICATED** → 앱 200 | 거부 뒤 앱 재읽기로 "객체 없음 403"과 구별 |
| SEC-01.1 다른 신원의 Secret 접근 | **PASS** | 앱 200 → 침입자 **403 PERMISSION_DENIED** → 앱 200 | 같은 버전 고정(`versions/1`) |
| ENC-02.1 Cloud KMS 키 | **PASS** (수정 후) | `enabled`, 키 버전 상태 `ENABLED` | 수정 전 첫 실행은 UNKNOWN(`UNEXPECTED_RESPONSE`). 아래 결함 1 |
| ISO-01.1 Workload Identity 분리 | **UNKNOWN**(`NOT_RUN`) | 객체 다리: 앱 · 다른 앱 자기 버킷 읽기 allow(전후), 교차 읽기 **deny** | DB 다리는 CNPG가 필요해 연결하지 않았다. 객체 다리만으로 PASS를 만들지 않는다 |
| SEC-01.3 로그의 비밀 | **PASS** | 카나리를 앱 env에 넣고 롤아웃 → 표식 기록 → env 제거. stdout · stderr 두 스트림 모두 표식 확인, 유출 0건 | 대기 약 20초 뒤 두 스트림 표식 확인 |
| SEC-01.3 유출 모의(`--simulate-leak`) | **FAIL**(기대대로) | 앱이 카나리를 stdout에 쓰면 `present`, 유출 1건 | 검색이 유출을 실제로 찾는지 확인한 음성 대조 |
| AUD-01.3 권한 변경 감사 로그 | **PASS** | 카나리 RoleBinding create · delete가 GKE Admin Activity 감사 로그(`cloudaudit.googleapis.com/activity`)에서 검색됨 | 생성 후 약 30초 안에 두 이벤트 모두 검색. 10분 마감 안 |
| AUD-01.1 · AUD-01.2 | UNKNOWN(`NOT_RUN`) | — | CNPG 로그가 필요하다. 다른 작업의 DB 네임스페이스는 건드리지 않았다 |
| IAM-01.2 | UNKNOWN(`NOT_RUN`, `UNSUPPORTED`) | — | GCP IAM 기준선 매핑 없음(A7 설계대로) |
| 나머지(ENC-01 · NET-01 · DR-01 · SEC-01.2 · SEC-01.4) | UNKNOWN(`NOT_RUN`) | — | 이번 범위 밖(종류 무관 시험 · DB 필요) |

## 추적 결과 요약 (A3 gcloud 어댑터, `a11-probe`)

수정 후 결과는 `trace-a11-probe.json`. gcloud 호출은 읽기 전용 허용 목록 9종(`projects get-iam-policy` · `projects describe` ·
`iam service-accounts get-iam-policy` · `asset search-all-iam-policies` · `secrets describe` · `storage buckets describe` · `kms keys describe`)만 썼다.

| 경로 | 찾은 자원 |
|---|---|
| Deployment/a11-app → KSA a11-app → GSA bravo-a11-app | 첨부 버킷(`objectViewer`, 비공개 · 균일 접근), Secret(`secretAccessor`, 사용자 관리 복제 · CMEK), Secret CMEK → KMS 키(`ENABLED`) |
| Deployment/a11-intruder → KSA a11-intruder → GSA bravo-a11-intruder | 다른 앱 버킷(`objectViewer`) |
| Deployment/a11-reader → KSA a11-reader → 직접 WI principal | 프로젝트 `logging.viewer`, Secret `secretmanager.viewer`, KMS 키 `cloudkms.viewer` |

수정 전 같은 대상 첫 실행은 자원 8개 · 메모 1개였다. 버킷 2개가 `gcp_resource`/`unknown`으로 분류됐고, KMS 키는 어느 경로에서도 나오지 않았으며,
a11-reader는 "Workload Identity 주석 없음"으로만 남았다.

## 발견한 결함과 수정

1. **ENC-02.1 gcp가 전역 Secret의 CMEK 키 버전을 못 읽음 (A7, 코드 결함).** 전역 Secret 버전은 키 버전을 `replicationStatus.userManaged.replicas[]`
   또는 `replicationStatus.automatic`에 담고, 최상위 `customerManagedEncryption`은 지역 Secret에만 있다. A7은 최상위만 읽어 CMEK가 켜진 Secret을
   `UNEXPECTED_RESPONSE`로 판정했다. 복제본마다 키 버전을 읽고 모두 등록 키에 속하고 `ENABLED`여야 `enabled`로 고쳤다. 증거 형식은 `format_version` 2
   (`key_version_sha256` · `key_version_states` 목록). 실측 응답 모양을 가짜 클라이언트 시험에 추가했다.
2. **GCS 버킷을 버킷으로 분류하지 못함 (A3, 코드 결함).** Cloud Asset 검색은 버킷을 `//storage.googleapis.com/<버킷>`으로 돌려준다.
   A3는 IAM 정책 형식 `projects/_/buckets/<버킷>`만 알아 `unknown`으로 남겼다. 두 형식을 모두 받는다.
3. **회원별 검색 결과를 버리면 권한이 빠짐 (A11 수정 중 발견).** 회원으로 거른 Asset 검색은 그 회원의 바인딩만 돌려준다. 같은 자원을 한 번만 남기면
   두 번째 주체의 권한이 사라진다(읽기 주체의 Secret 권한 누락). 모든 결과를 유지한다.
4. **CMEK 키와 직접 WI principal을 추적하지 못함 (A3, 기능 누락).** 앱은 KMS 바인딩 없이 Secret을 통해 키에 의존한다(권한은 Secret Manager 서비스 에이전트).
   추적기가 찾은 버킷 · Secret · 키를 메타데이터만 조회해 속성을 채우고, Secret의 `kms_key_name`에서 키를 추가한다. 주석 없는 KSA는
   `principal://.../subject/ns/<ns>/sa/<ksa>` 권한을 찾는다(프로젝트 번호는 `projects describe`).
5. **환경 관찰(코드 결함 아님).** GCS JSON API 오류 본문에는 google.rpc `status` 이름이 없다. 새 어댑터가 HTTP 상태의 표준 대응(401 → `UNAUTHENTICATED`,
   403 → `PERMISSION_DENIED`)으로 코드를 정한다. 익명 GCS 거부는 401로 왔고 A7 판정 규칙(익명은 401도 거부)과 맞았다.

## 남은 것

- ISO-01.1 DB 다리와 AUD-01.1 · AUD-01.2: GKE의 CNPG 목적지 DB가 준비되면 같은 도구에 DB 수집기를 연결해 실행한다.
- 실행 경로: 이번에는 운영자 노트북에서 `kubectl exec`로 파드 안 호출을 했다. 플랫폼 서버에서는 `metadata_token` 기반 `UrllibTransport`로
  서버 자신의 Workload Identity를 쓰는 방식이 맞다. 그 서버 등록(주체 · 프로젝트 고정)은 아직 없다.
- SEC-01.3 대상 앱은 `sleep` 대역이다. 실제 demo-chat을 GKE에 올리면 그 앱의 로그로 다시 확인한다.
- 시험 이미지 `python:3.12-slim`은 digest로 고정하지 않았다(A3 추적이 `digest_pinned: no`로 표시).
- `services/inventory/README.md`의 "GCP 어댑터 없음" 문구는 A3 범위라 고치지 않았다.

## 정리 방법

자원은 지우지 않았다. 지울 때는 프로젝트 · 계정 환경 변수를 맞춘 뒤 아래를 실행한다(`$P`는 프로젝트 ID, `$N`은 번호).

```sh
kubectl delete namespace a11-probe
gcloud storage rm -r gs://bravo-a11-ctl-attach-SUFFIX gs://bravo-a11-ctl-other-SUFFIX
gcloud secrets delete bravo-a11-ctl-db --project "$P"
gcloud iam service-accounts delete bravo-a11-app@$P.iam.gserviceaccount.com --project "$P"
gcloud iam service-accounts delete bravo-a11-intruder@$P.iam.gserviceaccount.com --project "$P"
# 읽기 주체의 프로젝트 바인딩
gcloud projects remove-iam-policy-binding "$P" --role roles/logging.viewer \
  --member "principal://iam.googleapis.com/projects/$N/locations/global/workloadIdentityPools/$P.svc.id.goog/subject/ns/a11-probe/sa/a11-reader"
# KMS 키링 · 키는 삭제할 수 없다. 키 버전만 파기 예약한다.
gcloud kms keys versions destroy 1 --key bravo-a11-ctl-secrets --keyring bravo-a11-ctl --location asia-northeast3 --project "$P"
# 필요 없으면 Cloud Asset API 사용 중지
gcloud services disable cloudasset.googleapis.com --project "$P"
```
