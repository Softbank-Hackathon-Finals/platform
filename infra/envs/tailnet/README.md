# tailnet 정책 · operator 연합 신원 (T48, ADR-0035)

tailnet 정책 전체와 환경별 Tailscale operator 연합 신원(WIF)을 이 스택 하나로 적용한다. 원본은 Git뿐이다. 저장된 Tailscale secret은 없다.

| 입력 | 원본 |
|---|---|
| 정책 | `infra/tailnet/compose_policy.py`가 `base-policy.json` + 생성 조각(`routes-policy*.json`) + `identities.json`의 operator 태그 소유자로 합성 |
| 연합 신원 | `infra/tailnet/identities.json`: 환경 → 그 클러스터의 ServiceAccount 발급자(`https`) |

```sh
python3 infra/tailnet/compose_policy.py --base infra/tailnet/base-policy.json \
  --identities infra/tailnet/identities.json --tailnet default infra/tailnet/routes-policy.json > /tmp/policy.json
cd infra/envs/tailnet
terraform init -backend-config=<비공개 backend 파일>
terraform plan -var policy_file=/tmp/policy.json -var-file=<identities.json을 identities 변수로 감싼 파일>
```

## 인증

provider는 `TAILSCALE_OAUTH_CLIENT_ID`(연합 신원 ID)와 실행 환경의 OIDC 토큰(`TAILSCALE_IDENTITY_TOKEN` 또는 provider의 자동 획득)으로만 인증한다. 이 첫 신원 하나는 tailnet 관리자가 만든다. 범위는 `policy_file`과 연합 신원 관리, 태그는 operator 태그들이다.

## 처음 적용 전에

`tailscale_acl`은 정책 **전체**를 바꾼다. 실수로 실제 정책을 덮어쓰지 않도록 `overwrite_existing_content = false`다. 순서:

1. 관리자가 실제 정책을 `base-policy.json`으로 옮긴다. 이메일은 넣지 않는다(사람 출처는 `autogroup:*`와 IdP 그룹 이름). 합성기가 이메일을 거부한다.
2. `terraform import tailscale_acl.this acl`
3. `terraform plan`의 정책 차이가 옮긴 내용과 같은지 확인한 뒤 적용한다.

그 뒤 콘솔에서 고친 내용은 다음 적용 때 덮어써진다.

## 연결 수명

- **합류:** `identities.json`에 환경을 더하는 PR → 적용 → 출력 `operator_identities.<env>`의 `client_id` · `audience`를 그 환경 operator Helm 값(`oauth.clientId`, `oauth.audience`)으로 쓴다. 둘 다 secret이 아니다.
- **분리:** 그 환경을 지우는 PR → 적용(연합 신원 삭제) → operator가 더 인증하지 못한다. 장치 삭제와 도달 실패 확인은 T48 분리 단계가 한다.

## 아직 없는 것

CI 계획 · 적용 작업(보호된 GitHub Environment 승인), 공개 발급자 게시, T45 연결은 다음 PR이다. 이 PR을 병합해도 아무것도 적용되지 않는다.
