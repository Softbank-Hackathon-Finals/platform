# T47 로그인 동등성 시험: 출발지 Cognito · 목적지 Keycloak

2026-11-08 05:43–05:45 KST. 출발지는 실제 Cognito 사용자 풀을 **읽기만** 해서, 목적지는 #652가 렌더링한 Keycloak을 이 Mac의 컨테이너로 띄워 같은 세 가지를 확인했다.

| 확인 | 출발지 Cognito (실제 계정, 읽기만) | 목적지 Keycloak (로컬 컨테이너) |
|---|---|---|
| 스스로 가입이 막힘 | **PASS** — 설정 `AllowAdminCreateUserOnly=true`, 로그인 화면에 가입 링크 0개, `/signup`은 오류 화면(400) | **PASS** — realm `registrationAllowed=false`, 로그인 화면에 가입 경로 없음, 가입 주소는 400 · 403 · 404 중 하나 |
| 정확한 redirect만 받음 | **PASS** — 등록한 주소만 로그인 화면으로 가고, 다른 주소 4가지는 `redirect_mismatch` | **PASS** — 정확한 콜백만 로그인 화면 200, 다른 주소 3가지는 400 |
| 토큰 없는 쓰기 401 | **NOT_RUN** — 출발지 앱에 로그인 코드가 아직 없다([아래](#토큰-없는-쓰기-401-not_run)) | **NOT_RUN** — 앱의 동작이라 이 Keycloak 시험 범위 밖이다 |

목적지 Keycloak은 아직 클러스터에 적용하지 않았다. 목적지 열은 같은 설정을 로컬에서 띄운 결과이고, 목적지 환경의 확인이 아니다([한계](#한계)).

## 출발지: 실제 Cognito (읽기만)

본인 SSO 세션으로 출발지 계정(`ap-northeast-2`)의 사용자 풀 하나를 읽었다. 대상은 #651로 11/7 22:08 KST에 만든 풀 · 로그인 도메인 · 브라우저용 공개 클라이언트다. 공개본에서는 풀 ID를 `<pool>`, 클라이언트 ID를 `<client>`, 로그인 도메인을 `<login-domain>`, Edge 공개 앱 주소를 `<앱 주소>`로 쓴다.

- **한 일:** `describe-user-pool` · `describe-user-pool-client` · `describe-user-pool-domain`, 발급자 문서 · 공개 키 GET, `/oauth2/authorize` GET 6회, `/signup` GET 1회, 그 응답이 보낸 로그인 · 오류 화면 GET 1회씩, 앱 `/api/info` · `/api/auth/config` GET.
- **안 한 일:** 사용자 생성, 가입 POST, 비밀번호 입력, 토큰 교환, 풀 · 클라이언트 변경, `terraform apply`. AWS와 클러스터에 쓴 것은 없다.

### 설정 읽기

| 항목 | 값 | 뜻 |
|---|---|---|
| `AdminCreateUserConfig.AllowAdminCreateUserOnly` | `true` | 스스로 가입 막힘. 계정은 운영자만 만든다 |
| `AccountRecoverySetting` | `admin_only` | 비밀번호 복구도 관리자만 |
| 클라이언트 비밀 | 없음 | 브라우저용 공개 클라이언트 |
| `AllowedOAuthFlows` | `code` | 로그인 화면을 거쳐 코드로만 받는다 |
| `ExplicitAuthFlows` | `ALLOW_REFRESH_TOKEN_AUTH` 하나 | 앱이 이름 · 비밀번호를 직접 받는 흐름이 없다 |
| `CallbackURLs` · `LogoutURLs` | `<앱 주소>/` 하나씩 | 돌아갈 곳은 이 주소뿐 |
| `WriteAttributes` | `locale`, `zoneinfo` | 사용자가 작성자 이름 속성을 바꿀 수 없다 |
| `PreventUserExistenceErrors` | `ENABLED` | 없는 사용자인지 응답으로 알 수 없다 |
| ID 토큰 유효 시간 | 60분 | |
| 로그인 도메인 | `ACTIVE`, 기본 호스팅 화면(`ManagedLoginVersion=1`), 사용자 지정 도메인 없음 | |
| 태그 `component` | `source` | |
| 사용자 수(추정) | 0 | 시연 계정은 아직 없다 |

### 발급자 문서 · 공개 키

- 발급자 문서 200: `issuer`가 `https://cognito-idp.ap-northeast-2.amazonaws.com/<pool>`이고, 로그인 · 토큰 주소는 `<login-domain>`, `jwks_uri`는 발급자 아래다. ID 토큰 서명은 RS256만.
- 공개 키 200: RSA · RS256 · `sig` 키 2개.
- 발급자 문서의 `response_types_supported`(`code`, `token`)는 풀 전체 값이고 `code_challenge_methods_supported`는 없다. 클라이언트의 제한은 아래 요청으로 확인했다. PKCE는 Cognito가 강제하지 않는다(#651 README에 적은 한계).

### 로그인 화면 요청

모든 요청에 `client_id=<client>`, `scope=openid profile email`, 매번 새 `state`, S256 `code_challenge`를 붙였다. 리다이렉트는 자동으로 따라가지 않았다.

| 요청 | `redirect_uri` | 응답 |
|---|---|---|
| `/oauth2/authorize`, `response_type=code` | `<앱 주소>/` (등록한 값) | 302 → `<login-domain>/login`. 그 화면 GET 200: 이름 · 비밀번호 칸, 가입 링크 0개 |
| 같은 요청 | `https://attacker.example/` | 302 → `<login-domain>/error?error=redirect_mismatch` |
| 같은 요청 | `<앱 주소>/callback` | 같음 |
| 같은 요청 | `<앱 주소>` (끝 `/` 없음) | 같음 |
| 같은 요청 | `http://`로 바꾼 같은 주소 | 같음 |
| `/oauth2/authorize`, `response_type=token` | `<앱 주소>/` | 302 → 앱 주소로 돌아가며 `error=invalid_request`, `error_description=unauthorized_client`. 토큰을 주소에 바로 싣는 방식을 거부한다 |
| `/signup` | `<앱 주소>/` | 302 → `<login-domain>/error`. 그 화면 GET 400, 가입 양식 없음 |

### 토큰 없는 쓰기 401: NOT_RUN

같은 시각 `<앱 주소>/api/info`는 세 번 모두 `env=aws`, `write_blocked=true`였고 `/api/auth/config`는 404였다. 지금 출발지 앱 이미지에는 로그인 코드(demo-app #28)가 없어 `AUTH_ISSUER`를 받을 곳이 없다. 게다가 쓰기 차단 중이라 어떤 쓰기도 로그인 확인 전에 503으로 끝난다. 그래서 토큰 없는 `POST /api/messages`를 보내지 않았다.

다시 확인할 조건: #28이 든 이미지가 출발지에 배포되고 `AUTH_ISSUER`가 들어가 `/api/auth/config`가 `enabled: true`를 돌려주며, 쓰기 차단이 풀려 있을 때. 로컬 Keycloak에서 토큰 없는 쓰기가 401인 것은 #28에서 확인했지만, 이번 실행의 결과가 아니다.

## 목적지: Keycloak 실제 시험 (로컬 컨테이너)

```sh
BRAVO_TEST_KEYCLOAK=1 python3 -m unittest discover -s scripts/tests -p test_target_identity.py -v
```

2026-11-08 05:44:38–05:44:48 KST, develop `eeaf02d2b9e9e7d03897d13adf5a47ea116c83a7`, 이 Mac의 Docker 29.6.2(arm64). 이미지는 `identity.py`가 고정한 Keycloak 26.0.7 digest이고, #652가 렌더링한 설정 · realm을 그대로 넣었다. Edge 대신 로컬 http 주소를 썼다. 시험이 끝난 뒤 컨테이너가 남지 않았다.

**20개 통과, 건너뜀 0** (렌더링 시험 14 + 컨테이너 시험 6). 컨테이너 시험이 확인한 것:

| 시험 | 확인한 것 |
|---|---|
| `test_only_the_exact_callback_opens_the_login_page_and_sign_up_is_closed` | 정확한 콜백만 로그인 화면 200, 화면에 가입 경로 없음. `https://evil.example/` · 앱 `/other` · 끝 `/` 없는 앱 주소는 400. 가입 주소(`/registrations`)는 400 · 403 · 404 |
| `test_realm_keeps_names_fixed_and_logs_in_by_name_only` | `registrationAllowed=false`, 이름 바꾸기 · 이메일 로그인 꺼짐, 모든 속성은 관리자만 고친다 |
| `test_pkce_is_required_and_only_s256` | PKCE 없음 · `plain`은 `invalid_request` |
| `test_implicit_and_password_grants_are_refused` | 토큰 직접 발급 거부, 비밀번호 grant는 `unauthorized_client` |
| `test_issuer_and_keys_are_what_demo_chat_expects` | issuer가 `<공개 주소>/realms/bravo`, S256 지원, RSA 공개 키 |
| `test_moved_account_needs_a_temporary_password_then_a_new_one` | `identity_users.py`로 옮긴 계정은 비밀번호가 없어 로그인되지 않고, 임시 비밀번호로 로그인하면 새 비밀번호를 정한 뒤 `preferred_username=visitor01`인 ID 토큰을 받는다 |

## 한계

- 목적지는 로컬 컨테이너다. 목적지 클러스터에 Keycloak을 적용하지 않았고(공유 클러스터 쓰기 없음), Edge 로그인 경로(T17)도 아직 없다. 이슈 완료 기준의 "목적지에서도 스스로 가입이 막혀 있다"는 클러스터 적용 뒤 같은 시험으로 다시 확인한다.
- 출발지의 가입 막힘은 설정과 화면으로 확인했다. 가입 API(`SignUp`)를 직접 불러 거부되는 것은 보지 않았다(쓰기 시도라 이번 범위 밖).
- 출발지 실제 로그인과 첫 비밀번호 변경은 시연 계정이 없어(사용자 0) 확인하지 않았다.

## 원본

비공개 원본은 저장소 밖에 두었다(파일 `0600`, 폴더 `0700`). 공개본에는 판정에 필요한 값만 옮겼다.

| 원본 | SHA-256 |
|---|---|
| `describe-user-pool.json` | `319ece6a35ebaa73066d0a3584663b846a97cd44e054d18a7335f9bb38cd7504` |
| `describe-user-pool-client.json` | `36a30d5601a353ac12b077217b56c6cbb7f433e313225dc3cbbcaf2c76b2c442` |
| `describe-user-pool-domain.json` | `6d1c5d48e260e43de6c5cf8a2f6c7ddba46e63bea58142f4cea7adb48d61cc30` |
| `discovery.json` | `0e2c45ffafef0f56329665433585070eeeaf6a0d6028458795fa697b5631f09d` |
| `jwks.json` | `303a73d206aef8a824ec578cc04c383273d39baba0155ee39ebd8219c10070aa` |
| `authorize-cases.json` | `3e8706c1b3218b167f62729409f66b4354f70f3b2f6689018eae68a70b09bd7d` |
| `app-info-1.body` | `913850c62aabca5f12ea03e35e3b6d677dca56f6687b4c00c5dd103dbd8d794a` |
| `app-auth-config.body` | `37ec4665a8102d115ffd1ac20dae94c98b4dac64b0c1a68228aa2a531caeb35d` |

시험한 소스(develop `eeaf02d2`):

| 파일 | SHA-256 |
|---|---|
| `scripts/tests/test_target_identity.py` | `e7b59309d5024ed2097dd2c85d583d08822e4ea18b8b06cabca191d3ee4edf15` |
| `infra/envs/target-workloads/identity.py` | `fc09e46777d93e8c3ddd1c68796e693d28c4ff5d32d1bd1f70ad487f8e428706` |
| `infra/envs/target-workloads/identity_users.py` | `055e374ccd4513fa47149ded635b306dd013cbece6deed5783b6283babee5f23` |
| `infra/envs/source-identity/main.tf` | `f114b8187fd3d2dba4cfe987edce8aea5397751812d3ef298c467ebde44589c5` |

관련: [ADR-0051](../../adr/0051-login-migration-cognito-keycloak.md), [출발지 사용자 풀](../../../infra/envs/source-identity/README.md), 설계 7.3.
