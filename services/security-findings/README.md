# bravo-findings (T33)

출처별 보안 탐지 원본(AWS Security Hub · GuardDuty, Google Cloud SCC, Falco, Kyverno, T5 통제 시험)을 OCSF 1.1 부분집합 하나로 정규화한다. 설계는 [`docs/security/detection-integration.md`](../../docs/security/detection-integration.md), 형식 결정은 [ADR-0001](../../docs/adr/0001-security-finding-format.md).

- **규칙의 원본은 데이터 파일이다.** 심각도 · 상태 · 클래스 · 범주는 [`docs/security/detection/mappings.json`](../../docs/security/detection/mappings.json)만 읽어 정한다. 코드에 규칙을 넣지 않는다.
- **결정적이다.** 같은 입력이면 항상 같은 출력. LLM을 쓰지 않는다.
- **결과는 스키마로 검증한다.** 어기면 `NormalizationError`를 던지고 저장하지 않는다.
- **입력은 최소화한다.** 허용 필드만 보관하고 자유 서술·명령행·excerpt를 제외한다. 알려진 비밀 패턴과 식별자를 가린다.
- **권한과 실패를 보존한다.** [보안 경계와 운영 인수 조건](../../docs/security/detection-hardening.md), [ADR-0005](../../docs/adr/0005-t33-security-boundary.md).

```python
from bravo_findings import Normalizer, Dropped, CollectionError, NormalizationError

finding = Normalizer().normalize(
    "aws_security_hub", raw, environment_id="env-aws", collected_time=now_ms
)
```

| 예외 | 뜻 | 수집기가 할 일 |
|---|---|---|
| `Dropped` | 버리는 원본 (Kyverno `skip`) | 무시 |
| `CollectionError` | 출처가 오류를 보고 (Kyverno `error`) | 수집 상태를 `FAILED`로 |
| `NormalizationError` | 필수 값 없음 · 스키마 위반 | 원본을 격리하고 수집 상태를 `FAILED`로 |

upsert 키는 `(bravo.source, bravo.environment_id, finding_info.uid)`다.

## 개발

```bash
uv sync
uv run pytest                    # 예시 원본 → golden 비교, 매핑 기대값, 마스킹
BRAVO_REGEN=1 uv run pytest      # 규칙을 바꾼 뒤 golden 재생성 (diff를 검토하고 커밋)
```

배포 시에는 `docs/security/detection/`을 이미지에 넣고 `BRAVO_FINDINGS_RULES_DIR`로 위치를 알려 준다.

## 수집 · 조회 API

```bash
BRAVO_FINDINGS_AUTH_FILE=/run/secrets/findings-auth.json \
BRAVO_FINDINGS_SOURCES='[{"source":"aws_security_hub","environment_id":"env-aws"},{"source":"falco","environment_id":"env-onprem"},{"source":"kyverno","environment_id":"env-onprem"}]' \
BRAVO_FINDINGS_DB=/data/findings.db \
uv run uvicorn bravo_findings.api:create_app --factory --host 127.0.0.1 --port 8091 --no-access-log
```

| 요청 | 하는 일 |
|---|---|
| `GET /v1/security/summary` | 환경 × 범주 칸마다 상태(`OK` · `UNKNOWN` 확인 불가 · `STALE` 오래됨 · `NONE` 없음)와 심각도별 미해결 건수 |
| `GET /v1/security/findings?environment_id=&category=&min_severity=&status=open\|all` | 정규화된 탐지 결과 목록 |
| `GET /v1/security/coverage` | 환경 × 탐지 능력 표 ("없음"을 숨기지 않는다) |
| `GET /v1/security/sources` | 출처별 수집 상태 (`SourceStatus` 스키마) |
| `POST /v1/security/ingest/{kyverno\|falco\|bravo_controls}` | 밀어 넣기 (`ingest` + 환경·출처 범위 Bearer 자격). heartbeat는 격리 보류를 해제하지 않음 |
| `POST /v1/security/webhooks/falco/{environment_id}` | falcosidekick 웹훅 (custom header로 토큰) |
| `POST /v1/security/maintenance/resolve-silent` | 폐기: 미인증 401, 조회 자격 410. 시간이 지나도 해결하지 않음 |

- 모든 조회 API도 `read` 자격이 필요하다. 다른 환경 요청은 403이고, 환경을 생략해도 허용된 환경만 반환한다.
- **미인증 요청은 401이고 탐지·수집 상태를 바꾸지 않는다.** 거부 감사는 남긴다. 수신이 끊기면 기준 시간(15분) 뒤 `STALE`로 드러난다.
- 끌어오기(AWS Security Hub, GuardDuty 직접, Google Cloud SCC)는 `collectors.run_once`를 주기 작업으로 부른다. 최초에는 전체 보존 범위를 조회하고 증분은 5분 겹친다. 커서는 페이지·정규화·저장까지 성공했을 때만 전진한다. 격리 기록이 있으면 `INGESTION_GAP`이며 커서를 유지한다.
- 온프렘 에이전트(`agent.collect`)는 클러스터 안에서 Kyverno PolicyReport를 읽고 Falco DaemonSet 준비 상태를 확인해 플랫폼으로 **바깥 방향으로만** 보낸다.
- 저장소는 플랫폼 DB(T10)가 정해지기 전까지 SQLite다(`store.py`). 테이블은 PostgreSQL로 옮길 수 있게 단순하게 둔다.

탐지 능력 표의 `available`은 등록된 기능의 제공 여부이며 정상 수집·위협 부재를
뜻하지 않는다. 공급자 조회가 미설정/비활성을 확인해 `NOT_ENABLED`를 기록한 출처는
그 기능의 제공 출처에서 제외한다. 같은 기능을 제공하는 다른 출처가 있으면 유지한다.
권한 거부·시간 초과는 미설정으로 추측하지 않고 `states`의 `FAILED`로 구분한다.
`/sources`의 오류 원인과 `/summary`의 `UNKNOWN` 계약은 유지하며, 이후 실제 수집이
성공해야 해당 출처가 다시 포함된다. UI는 `available`만으로 정상 색상을 표시하면 안 된다.

### 실제 데이터 확인 전 상태

| 출처 | 코드 · 테스트 | 실제 확인 |
|---|---|---|
| AWS Security Hub | 계정/리전·Hub 전후 대조, 페이지·SDK 형식 검증 | 11/6 실제 401을 FAILED로 저장·조회. 양성 탐지 인수 필요 |
| GuardDuty 직접 | SDK 형식·다중 페이지·계정/리전/탐지기 결속·실패 시 커서 유지 | 11/8 팀 계정 서울 리전에서 샘플 탐지 3건 수집 → `THREAT` 칸 `OK`, 계정 불일치로 끊으면 `UNKNOWN`([증빙](../../docs/evidence/t33-guardduty-live-2026-11-08/README.md)). 실제(비샘플) 탐지는 미검증 |
| Google Cloud SCC | v2 응답 형식 · 다중 페이지 · 범위(번호 parent) · 시간 창 결속 · 연합 자격만 · 실패 시 커서 유지 | GCP 프로젝트에서 SCC 활성 필요. 필터(`eventTime` 밀리초)와 탐지 수집은 미검증 |
| Kyverno · Falco | 예시 PolicyReport(두 형태) · Falco 이벤트 | 온프렘 클러스터에 설치 필요 (T2 · T30 · T9) |

### Security Hub 명시 수집

```bash
uv run python -m bravo_findings.securityhub_pull \
  --profile bravo-personal --account-id "$EXPECTED_AWS_ACCOUNT" \
  --region ap-northeast-2 --environment-id env-aws --database "$PRIVATE_FINDINGS_DB"
```

- `SecurityHubSource`는 기대 `account_id`·`region`을 필수로 받는다. 기존 인자 없는 호출은 허용하지 않는다. 등록 환경과 이 스코프의 대응은 신뢰된 호스트 설정이 소유하며, 사용자 요청에서 임의 값을 전달하지 않는다.
- STS 계정 대조 후 `securityhub:DescribeHub`·`securityhub:GetFindings` 읽기만 수행한다. 운영 역할에 추가 DescribeHub 권한이 없으면 실패하며 기존 설정을 자동 수정하지 않는다. 별도 비공개 DB로 검증한다.
- 계정·리전 필터, 각 ASFF 행의 스코프·시간·제품/ID, Hub ARN·구독 시각 전후 일치를 확인한다. 첫 경로는 commercial AWS 단일 계정·리전이며 조직 전체·다른 partition의 인수가 아니다.
- 빈 `Findings: []`와 누락된 Findings는 다르다. 부분 응답·중복 ID·순환 페이지·조회 중 변경이면 해당 묶음을 저장하지 않고 이전 커서를 유지한다. 상품/표준의 모든 탐지 범위가 활성이라는 뜻은 아니다.
- 200페이지·페이지100건·수집10,000건 상한, 호출 사이 단조 시계120초 예산을 적용한다. SDK 연결5초·읽기10초·최대3회가 개별 호출을 제한하며, 엄격한 전체 종료 기한은 실행 호스트가 별도로 강제해야 한다.
- 401 `InvalidAccessException`은 ACCESS_DENIED다. 서비스 비활성으로 추측하거나 탐지0건 정상으로 처리하지 않는다. GuardDuty 등록이 이미 있으면 명시 인계 전에는 수집을 거부하며 자동 대체하지 않는다.
- [11/6 실제 AWS 실패 저장 검증](../../docs/evidence/t33-securityhub-2026-11-06/README.md)을 완료했다. 양성 탐지·운영 스케줄러·인증 API/콘솔까지의 전체 인수는 별도다.

### GuardDuty 직접 수집

Security Hub를 사용하지 않기로 정한 환경에서 운영자가 명시적으로 선택하는 경로다. SDK 실패를 이유로 자동 전환하지 않는다. 같은 환경에 Security Hub 등록이 있으면 실행을 거부하며, 기존 기록의 삭제나 출처 전환은 수행하지 않는다.

```bash
uv run python -m bravo_findings.aws_pull \
  --profile bravo-personal --account-id "$EXPECTED_AWS_ACCOUNT" \
  --region ap-northeast-2 --environment-id env-aws --database "$PRIVATE_FINDINGS_DB"
```

- `--account-id`는 승인된 접속 설정의 기대 계정이다. STS에서 실제 계정을 대조한 뒤 수집한다. IAM 권한은 `guardduty:ListDetectors`, `GetDetector`, `ListFindings`, `GetFindings`만 필요하다(계정 확인은 STS). 탐지기·샘플·IAM 자원을 생성하지 않는다.
- SDK의 기본 TLS 인증서 검증을 유지하고 사용자 지정 endpoint·프록시를 적용하지 않는다. 연결 5초·읽기 10초·최대 3회 시도이며, 페이지 200개·한 번 수집 10,000건 상한 초과 시 실패한다.
- 실제 `ListDetectors`가 빈 결과이거나 `GetDetector`가 `DISABLED`인 경우에만 `NOT_ENABLED`다. 권한 거부·누락된 응답·부분 페이지는 별도 실패로 남는다. 실패 종료 코드 1, `FAILED`/`UNKNOWN`, 이전 커서를 보존한다.
- `AdditionalInfo.Value`의 JSON에서 boolean `sample`만 보관한다. 자유 서술 필드는 버리며 잘못된 샘플 표시는 격리한다. `Archived=true`는 `Suppressed`이고 해결 증거가 아니다.
- 신규 검증은 별도 DB를 사용한다. 기존 GuardDuty의 심각도 0 예시나 이전 형식으로 저장된 동일 시각 이벤트는 의미 충돌로 격리될 수 있다. 운영 데이터 자동 재작성·삭제·커서 초기화는 하지 않으며 기존 저장소 인계/재처리는 별도 승인·검증 대상이다.
- 현재 제공 범위는 1회 실행과 기존 저장소·조회 모델 연결이다. 운영 스케줄러, 서비스 배포 및 실제 양성 탐지→인증된 조회 API의 전체 연결은 아직 완료하지 않았다.

근거: [ListFindings](https://docs.aws.amazon.com/guardduty/latest/APIReference/API_ListFindings.html), [GetFindings](https://docs.aws.amazon.com/guardduty/latest/APIReference/API_GetFindings.html), [AdditionalInfo](https://docs.aws.amazon.com/guardduty/latest/APIReference/API_ServiceAdditionalInfo.html), [심각도](https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_findings-severity.html).

### Google Cloud SCC 수집

SCC v2 `ListFindings`(`{parent}/sources/-/locations/global/findings`)만 쓴다. 2024-12-09 이후 활성화한 조직은 v2만 지원하고, 데이터 상주를 켜지 않았으면 위치는 `global`이다. 지역 endpoint는 지원하지 않는다.

```bash
uv run python -m bravo_findings.gcp_pull --environment-id env-gcp \
  --scope-file "$PRIVATE/scc-scope.json" \
  --credentials-file "$PRIVATE/scc-wif.json" --database "$PRIVATE_FINDINGS_DB"
```

범위 파일(`--scope-file`, 기본 `BRAVO_SCC_SCOPE_FILE`)은 배포 때 Secret이나 ConfigMap으로 넣는다. 저장소가 공개라서 번호는 Git에 두지 않는다.

```json
{"env-gcp": {"parent": "projects/<번호>",
             "audience": "//iam.googleapis.com/projects/<번호>/locations/global/workloadIdentityPools/<pool>/providers/<provider>",
             "service_account": "<reader>@<project>.iam.gserviceaccount.com"}}
```

- **자격은 Workload Identity 연합만 받는다.** `external_account` 설정 파일에서 토큰 출처가 파일(플랫폼 발급자의 projected 토큰) 하나인 경우만 쓴다. 서비스 계정 키, 사용자 자격, `client_secret`, 명령 · URL 토큰 출처, workforce pool, 다른 STS 주소, 다른 universe는 수집 전에 거부되고 `ACCESS_DENIED`로 남는다. 서비스 계정 가장(impersonation)은 `iamcredentials.googleapis.com`의 `generateAccessToken` 주소만 받는다.
- **권한은 읽기 두 개다.** `securitycenter.findings.list`(예: `roles/securitycenter.findingsViewer`)와 `securitycentermanagement.securityCenterServices.get`(탐지 서비스 상태 읽기)를 그 parent에 준다. SCC 활성화, 소스, finding, mute 설정을 만들거나 바꾸지 않는다.
- **0건과 미확인을 구분한다.** SCC는 "찾은 것 없음"과 "탐지기가 돌지 않음"에 똑같이 `{}`를 돌려준다. 그래서 목록을 받기 전에 탐지 서비스(기본 `security-health-analytics`)의 `effectiveEnablementState`를 읽는다. `ENABLED`일 때만 빈 결과를 0건으로 믿는다. `DISABLED`이면 `NOT_ENABLED`(GuardDuty의 탐지기 없음과 같음)이고, 그 밖의 값이나 읽기 실패는 미확인(`SOURCE_UNHEALTHY`·`ACCESS_DENIED`)이다. 어느 경우든 커서를 움직이지 않는다.
- **범위는 운영자가 고르지 않는다.** 운영자는 `--environment-id`만 고른다. parent, 연합 provider(`audience`), 가장할 reader 서비스 계정은 그 환경의 범위 파일 값과 정확히 같아야 한다. 자격 설정이 다르거나 환경이 등록되지 않았으면 호출 전에 `ACCESS_DENIED`다. 그래서 reader가 읽을 수 있는 다른 조직·프로젝트의 결과가 이 환경으로 기록되지 않는다. parent는 번호(`projects/<번호>`, `folders/<번호>`, `organizations/<번호>`)만 받는다. 응답 finding 이름이 그 parent 아래가 아니면 `SOURCE_UNHEALTHY`다(AWS의 계정 대조에 해당).
- **응답 결속:** 같은 페이지 토큰 반복(페이지 200개 상한), 같은 finding 중복, `eventTime`이 조회 창 밖이거나 없음, 형식이 다른 응답은 `SOURCE_UNHEALTHY`이고 커서를 그대로 둔다. 빈 결과는 필드가 생략된 응답(`{}`)으로 온다.
- 환경의 프록시 · netrc 설정은 API와 토큰 호출 모두 쓰지 않는다(`trust_env=False`).
- **GCP 쪽 연합:** [`infra/envs/gcp-scc-reader`](../../infra/envs/gcp-scc-reader/README.md)가 플랫폼 발급자를 믿는 workload identity pool · provider와 읽기 전용 reader(`findingsViewer` · `securityCenterServicesViewer`)를 만들고, 범위 파일 · 자격 설정을 출력으로 낸다. 아직 적용하지 않았다.
- **아직:** SCC 활성과 플랫폼 주기 작업은 없다. 실제 프로젝트에서 SCC를 켠 뒤 확인할 것이 셋 있다.
  - 필터 문법: 코드는 `eventTime`을 쓰는데 문서 예시는 `event_time`이다.
  - finding 이름 형식: 조직 단위로 켜면 `projects/<번호>`로 조회해도 이름이 `organizations/<번호>/sources/...`로 올 수 있다. 그러면 지금 이름 대조가 모두 `SOURCE_UNHEALTHY`가 된다.
  - 탐지 서비스 상태 응답: `effectiveEnablementState`의 실제 값과, 조직에서 물려받은 경우의 응답.

  셋을 확인한 뒤 증빙을 남긴다.

근거: [v2 ListFindings](https://docs.cloud.google.com/security-command-center/docs/reference/rest/v2/projects.sources.locations.findings/list), [v2 이전 안내](https://docs.cloud.google.com/security-command-center/docs/migrate-v2-api).

## 호환성과 배포 주의

- `normalizer_version=2`: UID는 마스킹 전 식별자의 해시이며 golden 예시도 함께 갱신했다.
- 기존 `BRAVO_INGEST_TOKENS`는 사용할 수 없다. 자격 레지스트리 형식·0400/0600 권한·만료·회수는 보안 경계 문서를 따른다. 파일 설정이 없으면 모든 데이터 요청을 거부한다. 브라우저에 서비스 토큰을 전달하지 않는다.
- 영속 DB 경로가 없으면 시작하지 않는다. 데이터가 있는 과거 DB는 자동 변환·삭제하지 않고 시작을 거부한다.
- SQLite 감사는 불변 감사 저장소가 아니다. TLS, 요청률 제한, T10 신원·회수, 격리 보류 복구, T24 감사, 실제 출처 검증이 운영 인수 조건이다.
- 이 패키지의 수집 `OK`와 출처의 `PASS`는 T19 게이트 승인 근거를 대체하지 않는다.

## 플랫폼 배포

- 이미지: `Dockerfile`(빌드 컨텍스트는 저장소 루트, 매핑 규칙 `docs/security/detection`을 이미지에 넣음). 비루트 10001, 읽기 전용 루트, uvicorn이 TLS로 8443만 연다. `publish-security-findings-image`가 develop에서 검사 · 게시하고 digest 고정 PR을 연다.
- 묶음: `deploy/platform-gitops`(namespace `bravo-security`). SQLite는 정적 PV(1GiB). API 인스턴스는 하나(`Recreate`)이고, GuardDuty 수집 Job도 같은 DB에 쓴다(`BEGIN IMMEDIATE` · `busy_timeout`으로 직렬화). 두 Pod 모두 `fsGroupChangePolicy: OnRootMismatch`. 자격 레지스트리 Secret은 init 컨테이너가 서비스 UID 소유 0400 일반 파일로 옮긴다(Secret 볼륨 링크는 로더가 거부한다). 교체하면 Pod를 다시 띄운다.
- 전제: `infra/envs/platform-security`(namespace · PV · 기본 거부, TLS · 자격 Secret). 갖춘 뒤 플랫폼 Argo 값 `securityFindings.enabled`를 켠다.
- AWS GuardDuty 주기 수집: CronJob `bravo-findings-pull`(5분)이 플랫폼 웹 신원으로 읽기 전용 IAM 역할을 받는다(`infra/envs/platform-security`).
- 온프렘 수집 경로: T44 선언(`platform.json` tailnet-ingress `bravo-findings`, `target-dev.json` egress 그룹 `bravo-target-findings`). API NetworkPolicy는 그 장치의 proxy Pod만 받는다.
