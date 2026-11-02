# bravo-findings (T33)

출처별 보안 탐지 원본(AWS Security Hub · GuardDuty, Google Cloud SCC, Falco, Kyverno, T5 통제 시험)을 OCSF 1.1 부분집합 하나로 정규화한다. 설계는 [`docs/security/detection-integration.md`](../../docs/security/detection-integration.md), 형식 결정은 [ADR-0001](../../docs/adr/0001-security-finding-format.md).

- **규칙의 원본은 데이터 파일이다.** 심각도 · 상태 · 클래스 · 범주는 [`docs/security/detection/mappings.json`](../../docs/security/detection/mappings.json)만 읽어 정한다. 코드에 규칙을 넣지 않는다.
- **결정적이다.** 같은 입력이면 항상 같은 출력. LLM을 쓰지 않는다.
- **결과는 스키마로 검증한다.** 어기면 `NormalizationError`를 던지고 저장하지 않는다.
- **식별자는 저장 전에 가린다.** 접근 키 ID · 12자리 계정 ID · IPv4.

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
