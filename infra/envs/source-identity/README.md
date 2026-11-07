# T6 출발지 로그인: Cognito 사용자 풀

demo-chat 로그인용 Amazon Cognito 사용자 풀 하나를 만든다. 사용자 풀, 로그인 도메인, 브라우저용 공개 클라이언트 세 자원뿐이다.

**아직 만들지 않았다.** `terraform plan`과 `apply`를 실제 계정으로 돌린 적이 없다. 확인한 것은 [검증](#검증)에 있다.

## 왜 있나

데모 앱이 AWS 전용 로그인 서비스를 쓰게 해서, 플랫폼이 "관리형 인증"도 찾아 옮기는 장면을 만든다.
앱은 표준 OIDC(발급자 주소 + 공개 클라이언트 ID)만 알도록 만들었다([demo-app #28](https://github.com/Softbank-Hackathon-Finals/demo-app/pull/28)).
그래서 이 의존성은 **설정 교체** 등급이다. 목적지에서는 같은 규칙을 쓰는 Keycloak으로 주소만 바꾼다.

## 무엇을 왜 이렇게 만드나

| 결정 | 이유 |
|---|---|
| 비밀 없는 공개 클라이언트, `code` 흐름만 | 클라이언트 비밀을 브라우저에 둘 수 없다. **PKCE는 앱이 보내고 Cognito는 강제하지 않는다.** Cognito에는 공개 클라이언트에 PKCE를 요구하는 설정이 없어서, 새어 나간 code는 클라이언트 ID만으로 교환될 수 있다. demo-chat(`auth.js`)은 항상 S256 challenge를 보낸다 |
| `explicit_auth_flows`는 갱신 토큰만 | 앱이 비밀번호를 직접 받는 흐름(USER_PASSWORD · SRP · ADMIN)을 열지 않는다. 비밀번호는 Cognito 화면에만 입력된다 |
| 스스로 가입 막음, 복구는 관리자만 | 시연 계정은 운영자만 만든다. 이메일 발송 설정이 필요 없다 |
| 이름 속성은 클라이언트로 쓸 수 없음 | `write_attributes`는 `locale` · `zoneinfo`만이다(빈 목록은 "모두"로 읽힌다). scope에 `aws.cognito.signin.user.admin`이 없다. demo-chat은 `preferred_username` → `cognito:username` → `email` → `name` 순으로 작성자를 정하므로, 사용자가 이 속성을 바꾸면 다른 사람 이름으로 쓸 수 있다 |
| 부하 생성기용 클라이언트는 따로 | 로그인을 켠 상태의 T18 쓰기용 클라이언트가 필요하면 별도로 만든다. 이 브라우저 클라이언트에 비밀번호 흐름이나 admin scope를 더하지 않는다 |
| 사용자를 Terraform으로 만들지 않음 | `aws_cognito_user`의 임시 비밀번호가 state에 남는다. 시험이 이 자원을 막는다 |
| ID 토큰 60분 | 앱은 ID 토큰만 받는다. 짧을수록 새어 나간 토큰의 유효 시간이 줄어든다 |
| `deletion_protection = INACTIVE`, `component=source` | 데모 뒤 `terraform destroy`로 지운다. 비용은 출발지로 잡힌다 |
| 콜백은 앱 주소의 `/` | 출발지·목적지 앱이 같은 Edge 주소 뒤에 있으므로 한 주소면 된다 |

## 입력

| 이름 | 예 | 설명 |
|---|---|---|
| `account_id` | 비공개 | 배포 계정. 다른 계정이면 공급자가 거부한다 |
| `domain_prefix` | `bravo-demo-chat-login` | 로그인 도메인 접두사. 리전 안에서 유일해야 하고 `aws` · `amazon` · `cognito`를 넣을 수 없다 |
| `app_origins` | `["https://chat.example.com"]` | 앱의 공개 주소(경로 없음). `https`만 받는다 |
| `allow_local_callbacks` | `false` | 이 Mac에서 버릴 풀을 만들 때만 `true`로 `http://localhost` 콜백을 허용한다. 시연용 풀에는 쓰지 않는다 |

## 적용 (운영자)

```sh
cd infra/envs/source-identity
terraform init -backend-config=<운영자 backend 설정>
terraform plan -var-file=<비공개 tfvars> -out=plan.bin
terraform apply plan.bin
terraform output   # auth_issuer, auth_client_id, login_domain
```

출력의 `auth_issuer` · `auth_client_id`를 demo-chat의 `AUTH_ISSUER` · `AUTH_CLIENT_ID`로 넣는다. 둘 다 비밀은 아니지만 `user_pool_id` · `auth_issuer`는 운영 식별자이므로 공개 증빙에는 붙이지 않는다([공개 증빙 절차](../../../docs/public-evidence-policy.md)).

`apply` 뒤 한 번 확인한다: `explicit_auth_flows`가 갱신 토큰 하나뿐인 상태에서 호스팅 로그인 화면으로 로그인되는지, 첫 로그인의 비밀번호 변경이 되는지, demo-chat에 그 이름으로 메시지가 저장되는지.

### 시연 계정 만들기

시연 당일 아침, 시연을 맡은 운영자가 만든다. 임시 비밀번호는 3일 유효하고 첫 로그인에서 바꾼다.
비밀번호가 Terraform state · 셸 기록 · 프로세스 목록(`ps`)에 남지 않게, 값은 입력 프롬프트로 받고 명령 인자가 아닌 표준 입력으로 넘긴다.

```sh
read -rs TEMP_PASSWORD   # 화면에 보이지 않게 입력. 12자 이상, 대·소문자·숫자 포함, 따옴표·역슬래시 없이
printf '{"UserPoolId":"%s","Username":"%s","MessageAction":"SUPPRESS","TemporaryPassword":"%s"}' \
  "$(terraform output -raw user_pool_id)" visitor01 "$TEMP_PASSWORD" \
  | aws cognito-idp admin-create-user --cli-input-json file:///dev/stdin
unset TEMP_PASSWORD
```

`printf`는 셸 내장 명령이라 프로세스 목록에 인자가 보이지 않는다. 시연 계정 이름은 메시지 작성자로 그대로 보이므로 실명을 쓰지 않는다.

## 이사할 때 알아 둘 것

- **비밀번호는 옮길 수 없다.** Cognito는 비밀번호 해시를 내보내지 않는다. 사용자 목록(`list-users`)은 옮길 수 있지만, 목적지에서 비밀번호를 다시 정해야 한다.
- **전환 중에는 두 발급자를 믿는다.** 목적지 앱은 `AUTH_ISSUER`=목적지 Keycloak, `AUTH_ALSO_TRUST`=이 사용자 풀로 둔다. 목적지 앱은 이 풀의 공개 키(`cognito-idp.<region>.amazonaws.com`)를 HTTPS로 받아야 한다. 이사가 끝나면 `AUTH_ALSO_TRUST`를 지우고 이 스택을 `destroy`한다.
- 이 풀이 남아 있는 동안 출구 판정에서는 AWS 의존성으로 남는다.

## 검증

| 확인 | 결과 |
|---|---|
| `terraform fmt -check` · `validate` | 통과 |
| `terraform test` (모의 공급자) | 8개 통과: 비밀 없는 code 흐름, 비밀번호 흐름 없음, 콜백 · 로그아웃 고정, 사용자 존재 노출 막음, 스스로 가입 막음, 이름 속성 쓰기 · admin scope 없음, localhost 콜백은 명시할 때만, 출력값, 금지 접두사 · http 주소 거부, 선언 자원 목록 |
| 실제 계정 `plan` · `apply` | 하지 않음 |
| 앱의 실제 로그인 | Keycloak으로만 확인(demo-app #28). Cognito 실계정 로그인은 `apply` 뒤 확인한다 |
