# 통제 시험기 (T14)

T5의 평가 프로필에 따라 관찰 결과를 `PASS / FAIL / UNKNOWN`으로 판정하고
`ControlRun`을 만든다. 프로필의 필수 시험은 실행하지 못했어도 결과에서 빠지지 않는다.
게이트 비교·예외 승인·실행 허가는 T19/T10의 책임이다.

## 구현 범위

| 구성 | 현재 동작 | 검증 범위 |
|---|---|---|
| 공통 판정기 | 등록된 앱·환경·실행 ID·방법 버전 결속, T5 스키마 검사, 누락 시험 UNKNOWN | 단위 시험 |
| gitleaks 결과 어댑터 | 소스와 렌더된 설정 양쪽 보고서, 정확한 버전·수집 범위·종료코드 확인 | 합성 보고서 및 로컬 시험 입력 |
| Prowler / kube-bench 결과 어댑터 | 고정한 점검 ID·대상 집합과 개별 결과 비교 | 합성 보고서; 실제 IAM 점검 매핑 미확정 |

**T14 전체 완료가 아니다.** T6 출발지·T9 목적지와의 실제 연결, 나머지 하위 시험,
Prowler/kube-bench 실행과 실환경 매핑, 양쪽 환경 통제 비교는 남아 있다.
`full` 프로필의 결과 20행을 만드는 것과 20개 시험을 실제 수행하는 것은 다르다.
미등록 수집기는 `UNKNOWN / NOT_RUN`이다. T19는 이를 차단해야 한다.
T14 이슈의 선행 조건을 만족하거나 완료 처리한 것으로 보지 않는다.

## 호출 경계

이 패키지는 **신뢰된 서버 내부 라이브러리**다. 공개 결과 업로드 API가 아니다.

1. 호출 서버가 사용자·조직 권한과 등록된 앱·환경·자원·신원을 확인한다.
2. 승인된 배포본의 [T5 프로필](../../docs/controls/profiles.json)과
   [스키마](../../docs/controls/control-result.schema.json)를 `Catalog`에 전달한다.
   요청자가 기대값·필수 시험·스키마를 바꾸지 못하게 한다.
3. 서버가 시험 ID별 신뢰된 수집기를 등록한다. `Runner`가 실행마다 새 ID를 만들고,
   이 ID·앱·환경·시험에 맞는 관찰만 수용한다.
4. 수집 시각이 실제 호출 시간창 안에 있는지 확인하고 최대 24시간의 증거 만료를 부여한다.
   T19는 평가 시점의 만료 여부를 다시 검사한다.
5. 서버가 결과와 증거를 접근 통제된 변경 불가 저장소에 보존하고 T19에 참조를 전달한다.

JSON Schema 통과, 해시 일치, ARN 문자열 일치만으로 실제 신원·실행·승인을 증명하지 않는다.
LLM이 만든 결과나 업로드한 보고서를 신뢰된 수집기로 등록하면 안 된다.
이 패키지는 운영 자격 증명을 만들거나 역할을 인수하지 않는다.

```python
import json
from pathlib import Path
from bravo_controls import Catalog, Environment, Runner

# 아래 경로와 환경은 서버 배포 설정에서 고정한다.
controls = Path("docs/controls")
catalog = Catalog(
    json.loads((controls / "profiles.json").read_text()),
    json.loads((controls / "control-result.schema.json").read_text()),
)
runner = Runner(catalog, probes={})  # 연결 전에는 모든 필수 시험이 UNKNOWN
run = runner.run(
    profile_id="demo-gate", profile_version="1", app="demo-chat",
    environment=Environment("source", "source", "aws:eks/source"),
)
```

## 판정과 오류 처리

- 정상 관찰이 기대값과 같으면 PASS, 다르면 FAIL이다.
- 필수 허용 대조군이 실패하거나 생략되면 관찰값과 무관하게 UNKNOWN이다.
- 시험 미실행, 잘못된 수집 범위·방법·시간, 보고서 누락·중복·미지원 상태는 PASS가 아니다.
- 도구 예외의 원문·응답 본문을 결과에 복사하지 않는다. 실패 사유는 T5의 고정 코드만 쓴다.
- 시간 역행은 실행 묶음을 거부한다. DST 변화에도 증거의 UTC 유효기간을 늘리지 않는다.
- 동기 호출을 스레드로 감싼 뒤 시간 초과만 선언하지 않는다. 수집기가 실제 SDK·외부 명령의
  제한시간을 설정해야 한다. 호출 서버에는 전체 작업 예산과 취소 처리가 별도로 필요하다.

## 도구 보고서

`tool_reports.py`는 도구를 재구현하거나 실행하지 않는다. Prowler OCSF JSON,
kube-bench JSON, gitleaks JSON을 공통 관찰값으로 변환한다.

`ToolRun.complete`와 `scope`는 보고서 작성자가 주장하는 값을 받는 필드가 아니다.
신뢰된 수집기가 실제 실행 종료, 출력 미절단, 환경·입력 revision·설정·제외 규칙과
검사 범위를 확인한 뒤 설정한다. 보고서 원문 크기는 10 MiB, 배열은 100,000개로 제한한다.
gitleaks는 승인된 설정을 고정하고 앱 내부 allowlist·baseline 때문에 누락되지 않았는지도
실행기에서 확인해야 한다. 이 수집·실행기는 아직 포함하지 않았다.

Prowler/kube-bench 매핑은 도구·정확한 버전·범위·매핑 버전·필수 `(check ID, target)`
집합을 고정해야 한다. kube-bench에는 benchmark 버전도 필요하다. 테스트의 점검 ID는
합성 예시이며 운영 매핑이 아니다. 실제 출력으로 매핑을 확정하기 전 IAM-01.2는 NOT_RUN이다.

해시는 원문 바이트의 무결성 식별자다. 여러 보고서는 `bravo-control-reports-v1` 도메인과
각 원문의 8바이트 big-endian 길이를 순서대로 붙여 해시한다. 원문에는 민감한 값이 있을 수
있으므로 콘솔·PR에 저장하지 않는다. 공개 결과에는 고정 사유·건수·해시만 남긴다.

## 검증

```sh
uv sync --locked
uv run pytest
uvx ruff check .
uv build
```

기본 시험은 외부 네트워크나 AWS 자원을 사용하지 않는다. 실제 환경 시험은 T6/T9가 제공하는
등록 정보·최소 권한 클라이언트·시험 전용 객체·증거 저장소를 연결한 뒤 별도로 수행한다.
감사·회전·복원처럼 쓰기 효과가 있는 시험은 승인된 전용 자원과 정리 절차가 갖춰진 후 추가한다.

공식 출력 계약 출처는 `tool_reports.py`에 명시한다.
