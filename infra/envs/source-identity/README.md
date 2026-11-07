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
| 비밀 없는 공개 클라이언트, `code` 흐름만 | 브라우저가 PKCE(S256)로 로그인한다. 클라이언트 비밀을 브라우저에 둘 수 없다 |
| `explicit_auth_flows`는 갱신 토큰만 | 앱이 비밀번호를 직접 받는 흐름(USER_PASSWORD · SRP · ADMIN)을 열지 않는다. 비밀번호는 Cognito 화면에만 입력된다 |
| 스스로 가입 막음, 복구는 관리자만 | 시연 계정은 운영자만 만든다. 이메일 발송 설정이 필요 없다 |
| 사용자를 Terraform으로 만들지 않음 | `aws_cognito_user`의 임시 비밀번호가 state에 남는다. 시험이 이 자원을 막는다 |
| ID 토큰 60분 | 앱은 ID 토큰만 받는다. 짧을수록 새어 나간 토큰의 유효 시간이 줄어든다 |
| `deletion_protection = INACTIVE`, `component=source` | 데모 뒤 `terraform destroy`로 지운다. 비용은 출발지로 잡힌다 |
| 콜백은 앱 주소의 `/` | 출발지·목적지 앱이 같은 Edge 주소 뒤에 있으므로 한 주소면 된다 |

## 입력

| 이름 | 예 | 설명 |
|---|---|---|
| `account_id` | 비공개 | 배포 계정. 다른 계정이면 공급자가 거부한다 |
| `domain_prefix` | `bravo-demo-chat-login` | 로그인 도메인 접두사. 리전 안에서 유일해야 하고 `aws` · `amazon` · `cognito`를 넣을 수 없다 |
| `app_origins` | `["https://chat.example.com"]` | 앱의 공개 주소(경로 없음). 로컬 시험만 `http://localhost` 허용 |

## 적용 (운영자)

```sh
cd infra/envs/source-identity
terraform init -backend-config=<운영자 backend 설정>
terraform plan -var-file=<비공개 tfvars> -out=plan.bin
terraform apply plan.bin
terraform output   # auth_issuer, auth_client_id, login_domain
```

출력의 `auth_issuer` · `auth_client_id`를 demo-chat의 `AUTH_ISSUER` · `AUTH_CLIENT_ID`로 넣는다. 둘 다 비밀이 아니다.

### 시연 계정 만들기

비밀번호가 Terraform state · 셸 기록 · 파일에 남지 않게 만든다. 임시 비밀번호를 지정하지 않으면 Cognito가 만들어 주지만 이메일이 없으므로 아래처럼 운영자가 메모리에서 정해 넘긴다.

```sh
read -rs TEMP_PASSWORD   # 화면에 보이지 않게 입력. 12자 이상, 대·소문자·숫자 포함
aws cognito-idp admin-create-user --user-pool-id "$(terraform output -raw user_pool_id)" \
  --username visitor01 --message-action SUPPRESS --temporary-password "$TEMP_PASSWORD"
unset TEMP_PASSWORD
```

첫 로그인에서 새 비밀번호를 정하게 된다. 시연 계정 이름은 메시지 작성자로 그대로 보이므로 실명을 쓰지 않는다.

## 이사할 때 알아 둘 것

- **비밀번호는 옮길 수 없다.** Cognito는 비밀번호 해시를 내보내지 않는다. 사용자 목록(`list-users`)은 옮길 수 있지만, 목적지에서 비밀번호를 다시 정해야 한다.
- **전환 중에는 두 발급자를 믿는다.** 목적지 앱은 `AUTH_ISSUER`=목적지 Keycloak, `AUTH_ALSO_TRUST`=이 사용자 풀로 둔다. 목적지 앱은 이 풀의 공개 키(`cognito-idp.<region>.amazonaws.com`)를 HTTPS로 받아야 한다. 이사가 끝나면 `AUTH_ALSO_TRUST`를 지우고 이 스택을 `destroy`한다.
- 이 풀이 남아 있는 동안 출구 판정에서는 AWS 의존성으로 남는다.

## 검증

| 확인 | 결과 |
|---|---|
| `terraform fmt -check` · `validate` | 통과 |
| `terraform test` (모의 공급자) | 5개 통과: 비밀 없는 code 흐름, 비밀번호 흐름 없음, 콜백 고정, 스스로 가입 막음, 출력값, 금지 접두사 · http 주소 거부, 선언 자원 목록 |
| 실제 계정 `plan` · `apply` | 하지 않음 |
| 앱의 실제 로그인 | Keycloak으로만 확인(demo-app #28). Cognito 실계정 로그인은 `apply` 뒤 확인한다 |
