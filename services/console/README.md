# T21 이전 운영 콘솔

T10의 실제 `/v1/plans/{id}` · SSE · 승인 · 컷오버 · 재조회 API를 소비한다. 별도 상태 머신·게이트·실행기를 만들지 않는다. 이 패키지만으로 실제 AWS/RKE2 이전이나 금융 서비스 운영 자격을 충족하지 않는다.

## 연결 방법

호스트가 이미 구성한 T10 ASGI 앱 옆에 정적 화면을 제공한다.

```python
from bravo_console import ConsoleApp

# authenticated_t10_app: 기존 create_operator_cutover_app 등의 결과.
# 실제 IdP 검증, 서버 측 세션, 계획별 권한, T19/SHIFT authority가 모두 필요하다.
app = ConsoleApp(authenticated_t10_app, public_origin="https://console.example.invalid")
```

- `/console/`에서 등록된 계획 ID를 입력한다. 호스트가 검증한 동일 출처의 세션을 사용한다. 토큰·테넌트를 URL이나 localStorage에 넣지 않는다.
- 호스트의 안전한 로그인 세션을 연결해야 한다. 로그인 발급·OIDC 교환·자동 권한 부여는 제공하지 않는다. 원래 T10의 기본 deny-all을 바꾸지 않는다.
- 브라우저 전용 경계로 POST에는 고정 Origin · JSON · `X-Bravo-Console: 1`을 모두 요구한다. 이것은 인증 헤더가 아니며, T10의 실제 신원/권한 검사를 생략하지 않는다.
- HTTPS를 전제로 한다(루프백 시험만 HTTP). 리버스 프록시는 고정 Host를 보존한다. 임의 Forwarded 헤더로 신뢰 origin을 바꾸지 않는다.
- 호스트는 Secure/HttpOnly/SameSite 세션 쿠키, 로그인 CSRF, 재인증·만료·폐기, 세션 수명, TLS, 요청 빈도·동시 연결 제한을 책임진다. 이 패키지는 그러한 운영 구성이 완료됐다고 주장하지 않는다.
- 정적 파일은 정해진 파일만 제공하고, 모든 응답에 no-store/CSP/frame 차단을 적용한다. POST 본문은 64 KiB, JSON 응답은 128 KiB, SSE 프레임은 64 KiB로 제한한다.

## 환경·앱·계획 등록

화면의 등록 패널은 기존 T10 `POST /v1/environments`, `/v1/workloads`, `/v1/plans`를
호출한다. 호스트는 T10 Registry와 실제 Kubernetes 읽기 probe를 먼저 연결해야 한다.
연결되지 않은 API를 콘솔이 대신 구현하거나 임의 환경을 등록하지 않는다.

1. AWS 출발지와 온프렘 목적지를 서버에 등록된 `connection_ref` 별칭으로 등록한다.
   kubeconfig·클라우드 키·Secret 값·API URL을 입력하는 화면은 제공하지 않는다.
2. 소스 저장소 HTTPS 주소, 앱/IaC 상대 경로, 출발 환경 ID와 namespace로 앱을 등록한다.
3. 등록된 앱·목적지 ID와 평가 프로필 ID·버전을 명시해서 계획을 만든다.
   프로필 기본값은 없으며 서버의 tenant별 허용 목록에 있는 조합만 등록한다.
   요청과 영수증의 두 프로필 값이 같아야 반환된 계획 ID로 조회·SSE를 연결한다.
   첫 상태는 `COLLECT`이며 등록은 통제 PASS·승인·실행을 의미하지 않는다.

스냅샷의 프로필은 ID·버전 한 쌍으로 읽고, 같은 계획의 재조회나 승인·실행 재요청에서
바뀌면 거부한다. 두 필드가 모두 없는 기존 계획은 조회할 수 있지만 프로필을 추정해
채우지 않는다. 프로필이 필요한 새 계획은 기존 환경·앱 등록을 재사용해 별도로 등록한다.

서버가 반환한 환경/앱/계획 결속을 확인한 뒤 영수증을 표시한다. 읽기 권한 경고는 그대로
보여 주고, 읽기 검증 성공을 최소 권한 또는 운영 자격 충족으로 바꾸지 않는다.
응답 유실·기한 초과에는 입력과 멱등 키를 메모리에 고정한다. 사용자가 **같은 요청 결과 확인**을
눌러야 재요청하며 다른 등록은 그때까지 잠근다. 자동 재시도·브라우저 영구 저장은 없다.
새 탭이나 새로고침은 요청 키를 복원하지 않으므로, 불확실한 접수는 운영자가 서버 기록을
확인한 뒤 처리해야 한다. 401/403이면 영수증과 입력을 지우고 늦게 도착한 응답도 표시하지 않는다.

로컬 통합 시험은 실제 T10 등록 코드·SQLite 저장소·HTTP API를 사용하되, 신원과 Kubernetes
읽기 probe는 명시적 fixture다. 첫 환경 등록이 서버에 저장된 뒤 응답만 잃는 경우의 같은 키
복구, 등록→계획 조회, 권한 회수 후 화면 삭제를 브라우저에서 확인했다. 실환경 등록·SSO·
T19 정본·T18 관측·컷오버 연결 완료를 뜻하지 않는다.

## 화면과 실행 경계

- S2 **코드 변경 승인 기록**은 `REHEARSE`에서 서버의 `change` 승인 요청을 읽고
  계획·diff·이미지·목적지·만료·리허설 `run_id`·예상 버전을 그대로 전송한다.
  선택적 gate/context는 쌍으로 보존한다. 최신 snapshot과 연속된 SSE 이력을
  확인하지 못하면 버튼을 잠근다. 실제 T15 검증·hold·현재 사람 권한은 T10이 재검사한다.
- 변경 승인 영수증은 컷오버 실행 영수증에 저장하지 않는다. 변경 승인만으로
  T31 반영·배포·컷오버를 호출하지 않는다. 응답 유실에는 자동 재시도하지 않고
  **같은 요청 결과 확인**은 snapshot과 SSE 이력만 읽으며 승인 POST를 보내지 않는다.
  이력에 같은 변경의 승인이 있어도 이번 멱등 키의 접수 결과라고 단정하지 않는다.
  재승인이 필요하면 같은 후보·리허설·계획 버전·만료를 다시 대조한 뒤 사용자가
  확인란과 별도 승인 버튼을 눌러야 원래 본문·멱등 키로 전송한다. 현재 권한은 서버가 재검사한다.
  서버에 `change_service` 또는 검증 공급자가 없으면 거부된 상태를 표시한다.
  콘솔이 승인 요청·검증 PASS를 만들지 않는다.

- 계획 · source/target 등록 ID · 버전 · 상태 머신 · 게이트/증거 참조 · 이벤트 이력을 표시한다. 환경 ID 표시를 접속 성공으로 표시하지 않는다.
- SSE ID와 본문의 plan/sequence/type을 대조하고, 누락·변경된 재생·잘못된 상태 이력을 거부한다. 연결 끊김/오래된 snapshot/만료된 결속은 변경 버튼을 잠근다.
- 승인에는 plan/diff/image/target/gate/context/expiry와 예상 버전을 함께 보낸다. 승인 기록과 실제 실행은 별도의 클릭이며, 최종 권한은 T10이 다시 검사한다.
- 실행 POST 응답을 잃으면 성공/실패를 추정하지 않는다. **같은 요청 결과 확인**은 계획 snapshot과 SSE 전체 이력을 다시 읽는다. 요청 전 cursor 이후의 `SHIFT → CUTOVER` 이벤트가 있으면 그 서버 명령 ID로 T10 reconcile만 호출한다. 실행 submit을 다시 보내지 않는다. 이 조회는 이 계획의 접수 명령 조회이며 다른 운영자의 명령을 본인 요청의 성공으로 단정하지 않는다.
- 접수 이벤트가 없더라도 요청이 도착하지 않았다고 확정하지 않는다. 최신 snapshot·빠짐없는 이력·동일 계획/환경/버전·승인 이벤트/이미지/게이트/만료를 대조한 뒤, 사용자가 대상을 다시 체크하고 **승인된 전환 실행**을 명시적으로 확인해야 원래 본문·멱등 키로 전송한다. 서버는 현재 권한과 이미 접수된 claim을 다시 검사한다. 이벤트 누락·만료·권한 오류에는 실행하지 않는다. 불확실한 요청이 있어도 같은 계획 재연결은 허용하지만 다른 계획으로 이동하지 않는다. 자동 변경 재시도는 없다.
- 권한 회수(401/403)를 확인하면 기존 계획·승인·관측·이력을 화면에서 제거하고 진행 중인 읽기 결과의 복원을 차단한다. 이미 접수됐을 수 있는 변경 요청은 취소됐다고 추정하지 않으며 재인증 후 서버 기록으로 확인해야 한다.
- 재시작/새 탭에서 승인 이벤트만 보고 실행 권한을 복원하지 않는다. 별도 실행 결과 재조회는 읽기 방향의 T10 reconcile이다.
- 관측이 연결되지 않은 값은 미연결/미확인이다. 게이트의 과거 PASS는 현재 실행 허가가 아니다.
- 내려받기는 브라우저 조회 이력이며 서명된 감사 영수증이 아니다. 사용자 입력·서버 설명을 HTML로 삽입하지 않는다.

## 검증과 남은 연결

```sh
uv sync --locked
uv run ruff check .
uv run pytest
corepack pnpm install --frozen-lockfile
corepack pnpm lint
corepack pnpm test
corepack pnpm build
# 브라우저 실행 파일을 설치한 시험 환경에서
corepack pnpm test:browser
```

Python 시험은 실제 T10 HTTP/SSE/상태 저장·승인·접수 코드를 호출하며, 신원·사전 게이트·Executor 응답은 시험 fixture다. 브라우저 시험도 같은 시험용 호스트를 쓰며 실제 AWS 또는 RKE2 자원은 변경하지 않는다. `tests/browser_server.py`를 운영 서버로 배포하면 안 된다.

T10/T18 선행이 아직 완료되지 않아 T21 전체 시연 완료나 보드 Done으로 처리하지 않는다. 실환경에서의 환경/앱 등록 인수, 의존성 조회, 통제 보고서 정본·관측 권한 연결, 현재 계획의 승인 요청 발행, 팀 로그인·운영 권한, 실환경 컷오버와 팀 사용자 시연은 해당 담당 경로의 통합 자격을 확인해야 한다. 가중치·쓰기 공백을 화면 애니메이션으로 임의 생성하지 않는다.

## T37 조회 연결

`PlanObservations`는 T10 계획 조회의 실제 인증을 전후 두 번 확인하고, 추가 환경 관측 권한도 전후 재검사한다. 환경 전체의 지표이므로 **계획 읽기 권한만으로 환경 지표를 공개하지 않는다.** 호스트가 제공하는 추가 `authorize(scope, plan)`는 검증된 신원과 서버 정책으로 판단해야 한다.

```python
from bravo_console.observations import PlanObservations, T37Client

# 별도 Secret에서 읽은 토큰과 검증하는 TLS context는 서버에만 둔다.
client = T37Client("https://observability.example.invalid", server_secret, tls=verified_context)
views = PlanObservations(authenticated_t10_app, client,
    environments={"registered-source": "source", "registered-target": "target"},
    authorize=host_environment_observation_permission,
    # PgBouncer를 실제로 운영하는 등록 환경만 적는다. 없는 환경은 "해당 없음"으로 표시한다.
    pooled_environments={"target"})
app = ConsoleApp(authenticated_t10_app, public_origin="https://console.example.invalid", observations=views)
```

`pooled_environments`에 적은 환경은 T37 `/pgbouncer` 조회도 함께 읽어 커넥션 풀 상태를 표시한다. `PAUSED`는 컷오버 중의 정상 상태라 **쓰기 일시 정지**(대기 연결 수 포함)로 보여 주고, 실행·판정 조건으로 쓰지 않는다(Executor는 PgBouncer에서 직접 읽는다). 서버는 상태 라벨을 그대로 믿지 않고 행에서 다시 계산하며, 라벨과 행이 다르거나 신선도(조회 10초·수신 30초)를 벗어나거나 값이 불완전하면 `UNOBSERVABLE`로 내린다. 풀 조회가 실패해도 HTTP 관측 값은 숨기지 않는다. HTTP 행의 `last_received_seconds_ago`는 화면에 "n초 전 수신"으로 표시한다.

호스트 종료 시 `await client.aclose()`를 호출한다. 등록된 계획의 두 환경만 조회하고 임의 URL/PromQL/환경 선택은 받지 않는다. T37 토큰은 브라우저로 전달하지 않는다. 외부 HTTP 조회 전체 기한은 5초(개별 I/O 3초), 응답은 64 KiB로 제한한다. 압축 응답·중복 JSON 키·비UTF-8·비유한 숫자·과도한 중첩을 거부하며, 본문이 조금씩 계속 도착해도 전체 기한을 연장하지 않는다. 이 제한은 T37·T10·보고서의 권한 검증을 대신하지 않는다. 또한 scope 변경/권한 회수/응답 환경 불일치/미래·오래된 관측/NaN은 값을 노출하거나 정상으로 대신하지 않는다. 오류율·p95는 표시용이며 `for_gate_decisions: false`다. T36의 부하 창과 판정 기록은 별도 계약으로 유지한다.

## T19 보안 통제 보고서 조회 연결

`PlanControlReport`는 호스트에 등록된 T19 비교 보고서를 시험별 출발지·목적지 판정, 이유 코드, 예외 적용 여부로 투영한다. T5 스키마·프로필 사본은 배포 패키지에 포함하며 원본과의 일치·SHA-256을 시험한다. 별도의 통제 판정기를 만들지 않는다.

```python
from bravo_console.reports import PlanControlReport, RegisteredGateDocument

# 아래 두 함수는 호스트 구현이다. 요청자가 보고서·프로필·URL을 고르지 않는다.
# load는 현재 계획에 등록된 불변 보고서 bytes를 반환한다.
# authenticate는 서버의 발급자 신뢰·테넌트 소유권·계획 결속을 검증한다.
reports = PlanControlReport(authenticated_t10_app,
    load=host_load_registered_report, authenticate=host_verify_report_authority)
app = ConsoleApp(authenticated_t10_app,
    public_origin="https://console.example.invalid", controls=reports)
```

- 등록 문서는 `RegisteredGateDocument(plan_id, plan_digest, document, gate_phase="initial", application_id=None, authority_expires_at=None)`이며 보고서 원문은 최대 256 KiB다. 호스트의 `authenticate`는 비동기 함수이고 명시적인 `True`만 수용한다. 신뢰할 수 없는 클라이언트 필드나 단순 문자열 비교만으로 발급자를 인증하면 안 된다.
- T10 조회 권한·계획 snapshot과 보고서 권한을 조회 전후로 확인한다. 다른 앱·환경 쌍, 누락·중복 시험, 알 수 없는 프로필, 모순되는 전체 PASS, 중복 JSON 키·비유한 숫자·미래 판정은 표시하지 않는다. 저장소·검증 오류 원문은 공개 응답에 포함하지 않는다.
- 브라우저는 현재 게이트 ID·context digest·계획 digest가 일치하고 서버의 `read_valid_until` 이전인 보고서를 표시한다. 새 조회 응답은 `plan_version`·`plan_state`도 현재 snapshot과 대조한다. 권한 회수·계획/버전/상태 변경 또는 조회 기한 경과 시 표를 제거한다. 이는 최근에 **조회한 과거 판정**이며 증거가 지금도 유효하다는 뜻은 아니다.
- workflow의 게이트 이벤트가 아직 없는 초기 비교는 서버가 전후 인증한 같은 snapshot의 버전·상태가 있는 경우에만 표시한다. `initial`·Application ID 없음·COLLECT/ANALYZE/PROPOSE/REHEARSE/GATE에서만 허용하며 **초기 비교 (실행 허가 아님)**으로 구분한다. 기존 이벤트와 충돌하거나 반영 후 보고서인 경우 이 경로를 사용하지 않는다. 새 metadata가 없는 과거 응답은 기존 이벤트 일치를 계속 요구한다. 이 표는 `Model`의 승인·실행 조건에 사용하지 않는다.
- 서버와 브라우저는 T10 계획에 고정된 `profile_id`·`profile_version`도 보고서와 대조한다. 전체 20개 시험 계획에 3개 데모 보고서를 붙이거나 버전이 다른 보고서를 표시하지 않는다. 프로필이 없는 기존 계획은 일반 조회를 유지하지만 통제 보고서는 표시하지 않으며, 보고서에서 프로필을 역으로 채우지 않는다.
- `authorizes_cutover: false`를 고정한다. 예외 적용을 동등성 PASS로 바꾸지 않는다. 전체 컷오버 조건에는 별도의 데이터 준비·SHIFT·현재 권한·승인 검사가 필요하며 T10/T19가 담당한다.
- `report_digest`는 읽은 원문 bytes의 SHA-256이다. T19의 정규화된 서명·판정 fingerprint나 실행 승인 digest를 대신하지 않는다. 원문 질문·로그·증거 URL·승인자 개인정보는 화면 API에 반환하지 않는다.

### 기존 T10 저장소에서 읽기

`bravo-console[t10]`의 `T10ControlReports`는 기존 `GateRecords`에서 현재 계획에
결속된 보안 보고서를 읽는다. 별도 보고서 업로드·DB·판정기·인증 서버를 만들지 않는다.

```python
from starlette.requests import Request
from bravo_console.reports import PlanControlReport
from bravo_console.t10_reports import T10ControlReports

# 세 입력은 기존 운영 호스트가 구성한다. 요청 문자열로 권한을 등록하지 않는다.
# gate_records: 현재 gate:read 권한을 검사하는 T10 GateRecords.
# verified_operator_from_request: T10 API와 동일한 검증된 신원 → Principal 변환.
# t19_authenticate_report: GateService와 같은 (actor, context, RunSnapshot) 출처 검증.
source = T10ControlReports(gate_records,
    resolve_principal=lambda scope: verified_operator_from_request(Request(scope)),
    authenticate_report=t19_authenticate_report)
reports = PlanControlReport(authenticated_t10_app,
    load=source.load, authenticate=source.authenticate)
app = ConsoleApp(authenticated_t10_app,
    public_origin="https://console.example.invalid", controls=reports)
```

조회는 T10의 기존 계획 잠금을 사용한다. 내부 `_transaction(write=True)`는 잠금
프로토콜을 선택하기 위한 것이며 어댑터는 SELECT만 실행한다. 권한·컨텍스트를 쓰는
호스트도 같은 정본 DB와 잠금을 따라야 한다. 지연될 수 있는 읽기 복제본으로
권한을 확인하지 않는다. 저장된 계획 snapshot과 현재 보고서의 gate/context,
최초 또는 반영 후 단계, Application ID가 일치해야 한다. 보안 보고서가 없거나
원문·서명·발급자 신뢰가 바뀌면 반환하지 않는다.

출처 검증은 T19의 `gate-result` 범위·해시·기한 검사를 재사용한다. 현재 신원과
계획별 조회 권한도 다시 검사한다. DB 저장 성공이나 JSON hash만으로 인증하지 않는다.
출처 확인 중 권한이 회수되거나 마지막 검증 때 서명 기한이 지나도 거부한다.
콜백·DB 오류의 원문은 HTTP 응답에 포함하지 않는다. `load`와 두 번의 `authenticate`는
각각 현재 저장소·호스트 권한을 다시 읽고, 이미 읽은 문서를 캐시해서 신뢰하지 않는다.

발급자 키·서명 원문을 같은 계획 DB에서 읽어야 한다면 `authenticate_report` 대신
`authority_factory(view)`를 전달한다. 공급자는 현재 `view.c`와 인증된
`view.principal`에 연결한 T19 `Ed25519Authority`를 반환한다. 키·envelope 조회에
별도 DB 트랜잭션을 열지 않고 같은 계획 잠금 안에서 현재 등록·폐기·기한을 확인한다.
이 공급자와 반환된 검증기를 트랜잭션 밖에 보관하거나 캐시에 넣으면 안 된다.

```python
source = T10ControlReports(gate_records,
    resolve_principal=resolve_current_operator,
    authority_factory=load_current_report_authority)
```

`authenticate_report`와 `authority_factory`는 정확히 하나만 지정한다. 후자의
어댑터가 수행하는 서명 검증은 전자와 같은 `gate-result` purpose·context·보고서
지문·기한 검사를 사용하며, 생성자 반환만으로 출처를 인증하지 않는다. 발급자 등록이나
서명 생성 기능도 없다. 기존 콜백 방식은 유지하며 권한 회수·다른 context·서명 만료를
우회하는 fallback은 제공하지 않는다.

`tests/test_t10_reports.py`는 SQLite·별도 로컬 PostgreSQL의 실제 T10 저장·잠금과
Ed25519 검증으로 최초/반영 후 단계, 권한 회수·다른 tenant·변조·서명 폐기/만료·
현재 컨텍스트 변경과 오류 비노출을 검사한다. 신원·권한·키·시험 원문은 합성 fixture다.
운영 호스트의 실제 issuer·계획 권한·T14 인수와 배포는 별도로 확인해야 한다.

시험 호스트의 보고서와 신뢰 콜백은 명시적인 fixture다. 실제 T19 정본 저장소·발급자 인증 연결과 팀 환경 자격은 남아 있으며, fixture PASS를 실제 보안 통제 통과로 사용하지 않는다.

`tests/test_gate_interop.py`는 실제 T14 Runner → T19 GateService → T10 계획 저장 →
T21 HTTP 투영을 연결한다. 정상·목적지 실패·출발/목적 시험 누락·증거 만료 5개 경로에서
판정과 이유가 유지되고, 보고서 발급 권한을 회수하면 같은 문서도 공개되지 않는지 확인한다.
수집 관찰·호스트 신원은 명시적인 합성 fixture다. 이 시험은 실제 AWS/RKE2 통제 성공이나
운영 인증을 증명하지 않으며, 화면 조회 과정에서 Executor 호출은 0건이어야 한다.

## 실제 T37 관측 투영 시험

T22 하네스에 `--console-browser`를 함께 주면 같은 T21/T37 경로를 실제 Chromium과
WebKit에서 읽는다. 정상 수집 값, 목적지 Collector 중단 뒤 미확인 표시, 로그인 회수 뒤
표시 삭제와 변경 요청 0건을 검사한다. 브라우저용 신원·계획은 로컬 fixture이며
운영 SSO 또는 팀 환경 검증을 대신하지 않는다. 실행 권한은 연결하지 않는다.
임시 서버는 loopback의 OS 할당 포트만 사용하고 종료하며, T37 토큰은 브라우저에
전달하지 않는다. 스크린샷은 운영자가 지정한 비공개 결과 폴더에만 저장한다.

`testing/live_observations.py`는 운영 서버가 아닌 명시적 로컬 시험 도구다. T22의
`testing/observability_e2e.py --console <services/console>`에서 생성한 T37 HTTP 주소와
일회성 시험 토큰을 stdin으로 받아, 실제 T21 `PlanObservations`와 T10 계획 조회를 거친다.
토큰을 argv·결과·브라우저에 넣지 않는다. 계획 등록과 로그인/환경 권한은 시험 fixture이며,
관측값은 실행 중인 T37에서 조회한다. 승인·Executor는 연결하지 않는다.

수집 중 양쪽 환경 조회, 무인증 거부, 별도 환경 권한 회수, 세션 회수와 토큰 비노출을
검사한다. 목적지 Collector를 중단한 단계에서는 목적지 `UNOBSERVABLE`과 null 수치,
출발지 관측 유지도 확인한다. 이 시험은 화면용 API 투영이며 실제 IdP·브라우저 SSO나
팀 클라우드의 운영 권한 인수를 대신하지 않는다.

### 실행 응답 유실 회귀 검증

실제 T10 HTTP·SQLite와 Chromium에서 서버 접수 후 응답 유실은 Executor 제출 1건 유지·조회 1건,
서버 도착 전 503과 SSE 순번 누락은 결과 확인 후에도 제출 0건·조회 0건임을 확인한다.
후자는 이력 재동기화만으로 실행하지 않으며, 사용자의 별도 실행 확인 뒤에만 원래 본문·키로
제출 1건이 된다. 로그인·사전 승인 근거·Executor 응답은 로컬 fixture이며 실환경 컷오버는 아니다.

## T20 복귀 이력 소비 (조회 전용)

[T20 #222의 제안 계약](https://github.com/Softbank-Hackathon-Finals/platform/pull/222),
기준 커밋 `a6c6e995692ca80c0143d298912103643a30e1ef`의 `failback` 승인 결속,
`failback.recorded`, `DONE_ROLLED_BACK` 상태를 읽는다. API 생산자의 승인 요청/결정 연결,
권한 회수 및 공개 snapshot 계약은 T10/T20 책임이며 이 화면 변경이 대신하지 않는다.

- 기존 전환과 반대인 환경 쌍·계획 digest·기존 실행/epoch·무결성 영수증을 검증한다.
- `edge_pending`은 DB 복귀 검증 뒤 트래픽 복귀 확인 대기, `rolled_back`은 서버가 기록한 복귀 완료,
  `aborted`는 쓰기 전 중단으로 구별한다. 명령·승인·실행·무결성 근거와 복구 시간을 표시한다.
- 복귀 기록이나 SSE 승인만으로 기존 CUTOVER 실행 영수증을 복원하거나 재사용하지 않는다.
  복귀 승인·제출·Edge 재시도 버튼은 제공하지 않으며 서버의 운영 승인 경로를 사용해야 한다.
- 새 탭/재접속은 전체 이력을 다시 검사한다. 401/403이면 복귀 근거도 지우고 지연 응답을 차단한다.
- 브라우저의 복귀 시나리오는 명시적 HTTP/SSE 계약 fixture다. 실제 DB·Edge 복귀, 운영 권한,
  서비스 무중단 검증을 뜻하지 않는다. 기존 실제 T10 코드/SQLite 회귀도 함께 실행한다.


## T33 보안 탐지 요약 연결

`PlanSecurity`는 기존 T33 `/v1/security/summary`의 환경별 수집 상태·미해결 탐지 건수를
읽는다. T19의 보안 시험 결과나 전환 허가를 새로 판정하지 않는다. 환경 전체의 결과이므로
호스트는 계획 읽기 권한에 더해 **계획의 두 환경 전체에 대한 보안 조회 권한**을 확인해야 한다.

```python
from bravo_console.security import PlanSecurity, T33Client

client = T33Client("https://findings.example.invalid", server_secret, tls=verified_context)
security = PlanSecurity(authenticated_t10_app, client,
    environments={"registered-source": "source", "registered-target": "target"},
    authorize=host_environment_security_permission)
app = ConsoleApp(authenticated_t10_app,
    public_origin="https://console.example.invalid", security=security)
```

- `authorize(scope, plan)`는 검증된 호스트 신원·정책에 따른 동기 콜백이며 명시적인 `True`만
  허용한다. 계획 조회와 추가 권한을 외부 읽기 전후 재검사하고, 계획 digest·앱·환경 쌍이
  바뀌면 버린다. 다른 tenant의 환경을 재사용하는 매핑이나 무조건 허용하는 콜백은 금지한다.
- 호스트가 불변 등록 ID → T33 환경 ID의 일대일 매핑을 고정한다. 브라우저는 환경·URL·
  쿼리를 선택하지 못한다. 서비스 토큰은 해당 환경의 **read 전용**이어야 하며 브라우저에
  전달하지 않는다. 호스트 종료 시 `await client.aclose()`로 연결을 닫는다.
- 고정 HTTPS 서버의 CA·호스트명을 검증한다. HTTP는 루프백 시험 전용이다. T37과 같은
  제한된 JSON 전송기를 재사용하며 리다이렉트·압축·64 KiB 초과·중복 키·5초 초과를 거부한다.
- 응답은 7개 범주와 정확히 해당 환경 하나여야 한다. 미래/10초 초과 응답, 누락·중복 환경,
  잘못된 상태·수치는 `UNKNOWN`이다. 출처의 `OK`는 **수집 정상**이며 보안 PASS가 아니다.
  `UNKNOWN`·`STALE`·`NONE`의 과거 건수는 공개하지 않고 `null`로 반환한다.
- T21의 `/v1/plans/{id}/security`는 읽기 전용 `console-security.v1` 표시 응답이다.
  `for_gate_decisions: false`이며 환경 ID·side·7개 cells(state/counts)만 투영한다.
  탐지 원문·출처 cursor·자원 식별자·비밀·외부 URL은 반환하지 않는다. 사용자에게 보이는
  요약과 T33/T19 정본 계약을 구분한다.
- 화면은 현재 계획과 결속된 15초 이내 응답만 표시한다. 권한 회수·계획 전환·조회 실패 시
  이전 건수를 제거한다. `NONE`은 탐지 출처 없음, `STALE`은 오래됨, `UNKNOWN`은 확인 불가이며
  모두 0건과 구분한다. 정상 수집의 0건도 공격 부재 또는 전체 보호 수준 통과를 뜻하지 않는다.

검증은 실제 T33 인증·SQLite 저장·View/API와 실제 T10 계획 인가를 로컬로 연결했다.
관찰 값·자격·신원은 시험 fixture다. 서비스 토큰의 환경 제한·회수, 계획/환경 권한 회수,
잘못된 응답 거부, 브라우저 표시 삭제를 확인했다. 실제 클라우드 탐지 활성화·운영 수집기,
운영 T33 HTTPS 주소·read 자격·호스트 권한 연결은 별도 인수 대상이며, 이 변경만으로
T33 전체 완료 또는 실제 위협 탐지 성공으로 기록하지 않는다.

재대조 보고서를 제공하는 호스트는 저장된 T10 GateRecords에서 `gate_phase="post_apply"`와 성공한 `application_id`를 읽어 `RegisteredGateDocument`에 전달하고 두 인증 호출에서 이를 검증한다. 프로필 이름으로 단계를 추측하지 않는다. 최초 보고서는 계획의 `profile_*`, 반영 후 보고서는 `recheck_profile_*`와 대조하며, 어느 보고서도 실행 권한을 발급하지 않는다. 등록 UI는 재대조 프로필 쌍을 선택적으로 전송하고 영수증·snapshot·승인 재시도에서 보존한다.


### 통제 보고서의 표시 기한

`T10ControlReports`는 `accept`가 확인한 `VerifiedAuthority`들의 가장 이른 기한을
`RegisteredGateDocument.authority_expires_at`에 보존한다. 콜백 모드와 같은 트랜잭션의
`authority_factory` 모드 모두 적용한다. 직접 호스트 어댑터가 이 필드를 제공한다면
시간대가 있는 Python `datetime`이어야 한다. 알려진 기한이 없는 명시적 신뢰 콜백은
기존 호스트 책임을 유지하며 표시 기한을 최대 15초로 제한한다.

`PlanControlReport`는 현재 시각+15초와 알려진 기한 중 이른 값을 `read_valid_until`로
전달한다. 이미 만료됐거나 잘못된 타입·시간대인 기한은 거부한다. 응답 직전에도 기한과
시계 역행을 확인하며, 처리 지연으로 끝난 기한을 새 조회 시각으로 연장하지 않는다.

브라우저는 수신 시각 대신 `queried_at`·`read_valid_until`을 대조한다. 누락/잘못된 기한,
현재보다 미래인 조회, 15초를 넘는 표시 창과 만료된 응답은 표시하지 않는다. 같은 응답을
다시 받아도 기한을 갱신하지 않는다. 활성 화면은 기존 1초 렌더링 주기에 따라 표를 비운다.
서버와 정적 자산은 같은 패키지로 배포한다. 새 클라이언트가 이전 서버의 기한 없는 응답을
받으면 해당 표를 미연결로 처리하며, 다른 승인/실행 계약을 완화하지 않는다.

이는 **과거 판정을 현재 권한으로 읽은 표시의 기한**이다. T14 증거의 만료나 T19 승인
기한을 늘리지 않고, 알려지지 않은 미래 권한 회수를 미리 감지하는 기능도 아니다.
실제 승인·실행은 기존 T10 현재 권한·T19 검증을 계속 따른다.

[실제 DB·두 브라우저·변이·Linux 검증](../../docs/evidence/t21-control-read-deadline-2026-11-06/README.md).
