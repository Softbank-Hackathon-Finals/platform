# 통제 시험기 (T14)

T5의 평가 프로필에 따라 관찰 결과를 `PASS / FAIL / UNKNOWN`으로 판정하고
`ControlRun`을 만든다. 프로필의 필수 시험은 실행하지 못했어도 결과에서 빠지지 않는다.
게이트 비교·예외 승인·실행 허가는 T19/T10의 책임이다.

## 구현 범위

| 구성 | 현재 동작 | 검증 범위 |
|---|---|---|
| 공통 판정기 | 등록된 앱·환경·실행 ID·방법 버전 결속, T5 스키마 검사, 누락 시험 UNKNOWN | 단위 시험 |
| gitleaks 결과 어댑터·실행기 | 소스와 렌더된 설정 양쪽 보고서, 고정 도구·설정·스냅샷 범위·종료코드 확인 | 합성 보고서와 실제 8.30.1 로컬 시험 입력 |
| Prowler / kube-bench 결과 어댑터 | 고정한 점검 ID·대상 집합과 개별 결과 비교 | 합성 보고서; 실제 IAM 점검 매핑 미확정 |
| AWS Secret 시험 | 앱 역할과 다른 역할로 같은 현재 Secret 읽기 시도 (`SEC-01.1`) | SDK 인터페이스 모의 시험 |
| OpenBao Secret 시험 | 두 Kubernetes SA의 신원·같은 현재 Secret 읽기를 전후 확인 (`SEC-01.1`) | 합성 API 응답 + 실제 loopback TLS; 팀 OpenBao 연결 미검증 |
| AWS 객체 시험 | 앱 읽기 성공 후 동일 객체의 익명 일반 GET (`IAM-01.1`) | SDK 인터페이스 모의 시험 |
| 앱 → Pooler TLS 시험 | 정상 TLS, 다른 CA·hostname 거부, 평문 거부 (`ENC-01.1/2/3`) | 회귀 시험 및 로컬 kind의 실제 PgBouncer 접속 |

OpenBao 등록·인증 경계와 T9/T19 인계 조건은 [OpenBao 접근 시험](docs/openbao-access.md)을 따른다.

TLS 시험의 배치·자격증명 경계와 남은 팀 환경 검증은
[PostgreSQL TLS 안내](docs/postgres-tls.md)를 따른다.
기존 앱 경계를 사용하는 별도 수집기의 제한과 호스트 인수 조건은
[앱 프로세스 TLS 수집기](docs/app-process-tls.md)에 정리한다.

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
   백업처럼 선행 효과가 있는 시험은 서버가 먼저 등록·보존한 `RunContext`를
   `registered_context`로 전달할 수 있다. 백업 영수증의 실행 ID를 사후에 바꾸지 않는다.
   앱과 환경의 ID·role·ref가 다르면 수집 전에 거부하며, 이전 관찰의 시각은 허용하지 않는다.
   이 인자는 외부 요청이나 AI 결과에서 받지 않는다. 등록·프로필/계획 결속·실행권의 단일 claim·
   재시작 후 완료 결과 재사용은 호출 오케스트레이터의 책임이며 Runner가 대신 증명하지 않는다.
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
실행기에서 확인해야 한다. 아래 `GitleaksProbe`가 제한된 텍스트 스냅샷 경로를 제공한다.

Prowler/kube-bench 매핑은 도구·정확한 버전·범위·매핑 버전·필수 `(check ID, target)`
집합을 고정해야 한다. kube-bench에는 benchmark 버전도 필요하다. 테스트의 점검 ID는
합성 예시이며 운영 매핑이 아니다. 실제 출력으로 매핑을 확정하기 전 IAM-01.2는 NOT_RUN이다.

해시는 원문 바이트의 무결성 식별자다. 여러 보고서는 `bravo-control-reports-v1` 도메인과
각 원문의 8바이트 big-endian 길이를 순서대로 붙여 해시한다. 원문에는 민감한 값이 있을 수
있으므로 콘솔·PR에 저장하지 않는다. 공개 결과에는 고정 사유·건수·해시만 남긴다.

## gitleaks 실행 경로 (SEC-01.2)

`secret_scan.GitleaksProbe`는 서버가 준비한 소스 revision과 렌더된 설정을 각각 검사한다.
두 경로는 서버 소유의 동시 수정되지 않는 UTF-8 텍스트 디렉터리여야 한다. Git 이력·바이너리·
아카이브 전체를 검사했다고 주장하지 않는다. `.git`, 심볼릭 링크, 특수 파일, 빈 입력,
읽기 오류, 크기 제한 초과는 거부한다. 이 경로가 지원하지 않는 앱은 별도 자격 시험이 필요하다.

- 배포 설정이 승인한 Gitleaks **8.30.1 + 실행 파일 SHA-256**을 받는다. 실행 전 사본의
  해시와 버전을 확인한다. 해시 자체를 앱이 지정하게 해서는 안 된다.
- 고정 `useDefault=true` 설정, 비어 있는 ignore 파일, `--ignore-gitleaks-allow`를 사용한다.
  앱의 `.gitleaks.toml`, baseline, allow 주석으로 시험을 생략하지 못하게 한다.
- subprocess에는 AWS·모델·GitHub 자격 증명과 Gitleaks 설정 환경변수를 전달하지 않는다.
  앱 코드나 Git hook을 실행하지 않는다. 파일별 10 MiB, 총 100 MiB, 파일·디렉터리 각각
  10,000개를 제한하고 각 검사 시간은 기본 60초, 최대 120초다.
- 입력 파일별 해시·revision·설정·도구 해시를 수집 범위에 결속한다. 결과에는 건수와 해시만
  보존한다. 비밀 원문·파일명·주변 문맥이 있는 보고서는 비공개 임시 디렉터리에서 처리 후 삭제한다.
- 검사 오류·timeout·보고서 누락·종료코드 불일치는 깨끗한 결과가 아니다. 증거 저장 실패도
  성공으로 반환하지 않는다. Gitleaks 기본 규칙의 탐지 한계는 남는다.

이 실행기는 OS 샌드박스를 대신하지 않는다. 실행 호스트의 자격 증명 접근과 외부 통신 차단,
CPU·메모리·디스크 quota, 서버 소유 입력 준비는 격리 워커의 책임이다. 임시 보고서는 10 MiB
초과 시 거부하지만, 디스크 쓰기 자체의 상한은 호스트 quota로 강제해야 한다.

실제 도구 시험은 `BRAVO_TEST_GITLEAKS`에 승인한 바이너리 절대경로,
`BRAVO_TEST_GITLEAKS_SHA256`에 신뢰된 `sha256:…` 값을 지정하고
`uv run pytest tests/test_secret_scan.py`로 실행한다. 기본 CI는 외부 바이너리 없이 회귀 시험을
수행하고 실제 도구 시험만 skip한다. 로컬 실제 시험은 합성 비밀 검출, 앱 allow 주석·설정 무시,
정상 입력의 탐지 0건을 검증한다. 운영 앱·렌더 산출물의 실제 SEC-01.2 증거는 별도다.

도구 옵션은 [Gitleaks 8.30.1](https://github.com/gitleaks/gitleaks/tree/v8.30.1)을 기준으로 한다.

## AWS 읽기 전용 시험

AWS 사용 시 `uv sync --extra aws`로 설치한다. 서버가 각각의 역할로 인증한 SDK 클라이언트를
주입한다. `SecretTarget` / `ObjectTarget`도 서버 등록 정보에서 가져온다.

- `SecretAccessProbe`: 앱 역할과 별도 역할의 같은 `AWSCURRENT` Secret 읽기. 응답 ARN,
  버전과 stage가 등록 정보와 다르면 UNKNOWN이다. Secret 값은 증거에 저장하지 않는다.
- `S3AnonymousReadProbe`: 앱의 일반 GetObject 성공 후 `botocore.UNSIGNED` 클라이언트로
  익명 일반 GetObject를 시도한다. **요청에 VersionId를 넣지 않는다.** 그러면 다른 권한인
  GetObjectVersion 시험으로 바뀌기 때문이다. 대신 응답 VersionId가 등록된 현재 버전과
  같은지 확인한다. 현재는 버전 관리된 객체만 지원한다. 응답 Body는 읽지 않고 닫는다.
- S3 익명 요청이 거부된 뒤 앱 역할로 같은 버전을 다시 읽어 존재를 확인한다.
  객체 삭제도 익명 요청에는 403으로 보일 수 있기 때문이다. 전후 대조군 중 하나라도
  실패하면 UNKNOWN이며 `postpositive`를 증거에 보존한다. 이는 정책과 객체의 원자적
  스냅샷을 보장하지 않으므로 시험 전용 객체와 승인된 시험 창이 필요하다.
- 정확한 AccessDenied 계열 코드와 HTTP 상태를 함께 확인한다. Secrets Manager는 해당
  오류에 400도 사용한다. 403이라는 숫자만으로 잘못된 서명·만료 인증을 정상 거부로 세지 않는다.
- 연결·읽기 timeout은 각각 10초 이하, standard 재시도는 총 2회 이하만 허용한다.
  이 설정은 요청마다 다시 확인한다. 전체 작업의 종료 시한을 보장하는 기능은 아니다.
- `evidence_sink(bytes)`가 성공해야 결과를 반환한다. 보존한 정규 JSON과 결과의 해시를
  연결한다. 저장소의 실제 내구성·소유권·보존 정책은 호출 서버가 검증해야 한다.

역할 ARN 문자열은 SDK의 실제 호출 신원을 증명하지 않는다. 서버는 별도 신원 확인 후
클라이언트와 역할을 연결해야 한다. SDK wire/debug 로그를 금지하고, 사용자 코드가 없는
시험 워커에서 실행한다. 실제 AWS 자격 시험은 수행하지 않았다.

`QualifiedAwsProbe`는 이 신원 결속을 수행하는 선택적 호스트 연결부다. 등록된 역할의
고유 `RoleId`와 STS 신원을 대조하고, 같은 임시 자격 증명으로 실제 통제 요청을 실행한 뒤
신원 확인 근거와 probe 증거를 함께 보존한다. [연결 계약과 실환경 인수 항목](docs/aws-qualification.md)을
따른다. Credential Provider·팀 로그인·최소 권한 발급은 호스트의 책임이다.

## 네트워크 대조 시험 (T14)

DB 5432·Pooler 6432 모두에 전후 허용 대조와 시험 주체의 정상 대조를 연결한다.
접속 시간 초과만으로 PASS를 만들지 않는다. [계약·배치·검증 범위](docs/network-controls.md)를 따른다.

## CNPG 백업 완료 읽기 검증 (T35)

등록된 자원 UID·요청 전 설정·컨트롤러 완료 상태를 대조한다. 백업 생성과 복원 데이터
검증은 별도 단계다. [계약·통합 경계](docs/cnpg-readback.md)를 따른다.

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

공식 출력 계약 출처는 `tool_reports.py`에 있으며, AWS 권한 차이는
[GetObject API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html),
오류 규칙은 [Secrets Manager Common Errors](https://docs.aws.amazon.com/secretsmanager/latest/apireference/CommonErrors.html)를 따른다.

환경 내부 OTLP 감사 기록 연결: [등록 원천·수신 시각·전체 export 검증](docs/otlp-audit.md).
T34의 환경 내부 OTel 수집 구성·중단 복구와 배포 전제는 [감사 전달 구성](docs/audit-forwarding.md)을 따른다. 설정 생성과 로컬 도구 실험은 양쪽 운영 환경의 감사 검색 완료가 아니다.

실제 로컬 DB·Kubernetes 감사 발생부터 수집·검색까지: [자격 시험](docs/live-audit-qualification.md). 팀 환경 완료와 구분한다.

백업 상태 조회자의 최소 권한: [지정 자원 GET과 실제 거부 시험](docs/backup-rbac.md).
