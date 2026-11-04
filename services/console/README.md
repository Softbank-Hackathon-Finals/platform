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
3. 등록된 앱과 목적지 ID로 계획을 만들면 반환된 계획 ID로 조회·SSE를 연결한다.
   첫 상태는 `COLLECT`이며 등록은 통제 PASS·승인·실행을 의미하지 않는다.

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

- 계획 · source/target 등록 ID · 버전 · 상태 머신 · 게이트/증거 참조 · 이벤트 이력을 표시한다. 환경 ID 표시를 접속 성공으로 표시하지 않는다.
- SSE ID와 본문의 plan/sequence/type을 대조하고, 누락·변경된 재생·잘못된 상태 이력을 거부한다. 연결 끊김/오래된 snapshot/만료된 결속은 변경 버튼을 잠근다.
- 승인에는 plan/diff/image/target/gate/context/expiry와 예상 버전을 함께 보낸다. 승인 기록과 실제 실행은 별도의 클릭이며, 최종 권한은 T10이 다시 검사한다.
- POST 응답을 잃으면 성공/실패를 추정하지 않는다. 메모리에 보존한 동일 본문·멱등 키로 사용자가 조회를 재시도한다. T10은 기존 실행 claim을 읽고 Executor에 두 번째 POST를 보내지 않는 계약이다. 브라우저 자동 변경 재시도는 없다.
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
    authorize=host_environment_observation_permission)
app = ConsoleApp(authenticated_t10_app, public_origin="https://console.example.invalid", observations=views)
```

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

- 등록 문서는 `RegisteredGateDocument(plan_id, plan_digest, document)`이며 보고서 원문은 최대 256 KiB다. 호스트의 `authenticate`는 비동기 함수이고 명시적인 `True`만 수용한다. 신뢰할 수 없는 클라이언트 필드나 단순 문자열 비교만으로 발급자를 인증하면 안 된다.
- T10 조회 권한·계획 snapshot과 보고서 권한을 조회 전후로 확인한다. 다른 앱·환경 쌍, 누락·중복 시험, 알 수 없는 프로필, 모순되는 전체 PASS, 중복 JSON 키·비유한 숫자·미래 판정은 표시하지 않는다. 저장소·검증 오류 원문은 공개 응답에 포함하지 않는다.
- 브라우저는 현재 게이트 ID·context digest·계획 digest가 일치하고 조회가 15초 이내인 보고서만 표시한다. 권한 회수 시 표도 제거한다. 이는 최근에 **조회한 과거 판정**이며 증거가 지금도 유효하다는 뜻은 아니다.
- `authorizes_cutover: false`를 고정한다. 예외 적용을 동등성 PASS로 바꾸지 않는다. 전체 컷오버 조건에는 별도의 데이터 준비·SHIFT·현재 권한·승인 검사가 필요하며 T10/T19가 담당한다.
- `report_digest`는 읽은 원문 bytes의 SHA-256이다. T19의 정규화된 서명·판정 fingerprint나 실행 승인 digest를 대신하지 않는다. 원문 질문·로그·증거 URL·승인자 개인정보는 화면 API에 반환하지 않는다.

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
