# operator 연합 신원 (T48, ADR-0035)

환경별 Tailscale operator 연합 신원(WIF)을 이 스택이 만든다. 원본은 `infra/tailnet/identities.json`뿐이다. 저장된 Tailscale secret은 없다.

tailnet 정책은 이 스택이 쓰지 않는다. 정책을 쓰는 곳은 `tailnet-policy` 워크플로([`infra/tailnet/README.md`](../../tailnet/README.md)) 하나다. 두 곳에서 쓰면 서로 덮어쓴다.

| 입력 | 원본 |
|---|---|
| 연합 신원 | `infra/tailnet/identities.json`: 환경 → 그 클러스터의 ServiceAccount 발급자(`https`) |
| 태그 소유자 | `tag:bravo-<env>-operator`는 신원보다 먼저 정책에 있어야 한다. `tailnet-policy` 조각으로 넣는다. |

```sh
cd infra/envs/tailnet
terraform init -backend-config=<비공개 backend 파일>
terraform plan -var-file=<identities.json을 identities 변수로 감싼 파일>
```

## 인증

provider는 `TAILSCALE_OAUTH_CLIENT_ID`(연합 신원 ID)와 실행 환경의 OIDC 토큰(`TAILSCALE_IDENTITY_TOKEN` 또는 provider의 자동 획득)으로만 인증한다. 이 첫 신원은 tailnet 관리자가 만든다. 범위는 연합 신원 관리(`federated_keys`)만 둔다. 정책 쓰기 범위는 주지 않는다.

## 연결 수명

- **합류:** 그 환경의 태그 소유자를 `tailnet-policy` 조각에 넣는다 → `identities.json`에 환경을 더하는 PR → 적용 → 출력 `operator_identities.<env>`의 `client_id` · `audience`를 그 환경 operator Helm 값(`oauth.clientId`, `oauth.audience`)으로 쓴다. 둘 다 secret이 아니다.
- **분리:** 그 환경을 지우는 PR → 적용(연합 신원 삭제) → operator가 더 인증하지 못한다. 정책 항목 회수는 `tailnet-policy`의 prune(T48 후속)이 한다. 장치 삭제와 도달 실패 확인은 T48 분리 단계가 한다.

## 아직 없는 것

CI 계획 · 적용 작업과 그 자격(보호된 GitHub Environment, state 접근 역할), 공개 발급자 게시, T45 연결은 다음 PR이다. 이 PR을 병합해도 아무것도 적용되지 않는다.
