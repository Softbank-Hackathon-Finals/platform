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

T10/T18 선행이 아직 완료되지 않아 T21 전체 시연 완료나 보드 Done으로 처리하지 않는다. 실제 환경/앱 등록 API, 의존성·통제 비교의 상세 조회, 관측 권한 연결, 현재 계획의 승인 요청 발행, 팀 로그인·운영 권한, 실환경 컷오버와 팀 사용자 시연은 해당 담당 경로의 통합 자격을 확인해야 한다. 가중치·쓰기 공백을 화면 애니메이션으로 임의 생성하지 않는다.

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

호스트 종료 시 `await client.aclose()`를 호출한다. 등록된 계획의 두 환경만 조회하고 임의 URL/PromQL/환경 선택은 받지 않는다. T37 토큰은 브라우저로 전달하지 않는다. 외부 조회 총 기한·응답 크기를 제한하고, scope 변경/권한 회수/응답 환경 불일치/미래·오래된 관측/NaN은 값을 노출하거나 정상으로 대신하지 않는다. 오류율·p95는 표시용이며 `for_gate_decisions: false`다. T36의 부하 창과 판정 기록은 별도 계약으로 유지한다.
