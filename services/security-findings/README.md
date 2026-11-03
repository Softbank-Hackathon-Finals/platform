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
- 끌어오기(AWS Security Hub, Google Cloud SCC)는 `collectors.run_once`를 주기 작업으로 부른다. 최초에는 전체 보존 범위를 조회하고 증분은 5분 겹친다. 커서는 페이지·정규화·저장까지 성공했을 때만 전진한다. 격리 기록이 있으면 `INGESTION_GAP`이며 커서를 유지한다.
- 온프렘 에이전트(`agent.collect`)는 클러스터 안에서 Kyverno PolicyReport를 읽고 Falco DaemonSet 준비 상태를 확인해 플랫폼으로 **바깥 방향으로만** 보낸다.
- 저장소는 플랫폼 DB(T10)가 정해지기 전까지 SQLite다(`store.py`). 테이블은 PostgreSQL로 옮길 수 있게 단순하게 둔다.

### 실제 데이터 확인 전 상태

| 출처 | 코드 · 테스트 | 실제 확인 |
|---|---|---|
| AWS Security Hub | botocore Stubber로 ASFF 응답 형식까지 검증 | AWS 계정 · Security Hub 활성화 필요 (T1) |
| Google Cloud SCC | 가짜 HTTP 세션 | GCP 프로젝트 · SCC 필요. 필터 문법 확인 필요 |
| Kyverno · Falco | 예시 PolicyReport(두 형태) · Falco 이벤트 | 온프렘 클러스터에 설치 필요 (T2 · T30 · T9) |

## 호환성과 배포 주의

- `normalizer_version=2`: UID는 마스킹 전 식별자의 해시이며 golden 예시도 함께 갱신했다.
- 기존 `BRAVO_INGEST_TOKENS`는 사용할 수 없다. 자격 레지스트리 형식·0400/0600 권한·만료·회수는 보안 경계 문서를 따른다. 파일 설정이 없으면 모든 데이터 요청을 거부한다. 브라우저에 서비스 토큰을 전달하지 않는다.
- 영속 DB 경로가 없으면 시작하지 않는다. 데이터가 있는 과거 DB는 자동 변환·삭제하지 않고 시작을 거부한다.
- SQLite 감사는 불변 감사 저장소가 아니다. TLS, 요청률 제한, T10 신원·회수, 격리 보류 복구, T24 감사, 실제 출처 검증이 운영 인수 조건이다.
- 이 패키지의 수집 `OK`와 출처의 `PASS`는 T19 게이트 승인 근거를 대체하지 않는다.
