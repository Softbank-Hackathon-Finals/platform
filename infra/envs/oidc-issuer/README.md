# 공개 OIDC 발급자 (T48, ADR-0035)

인터넷에서 닿지 않는 클러스터(온프렘 RKE2, GCE RKE2)의 ServiceAccount 발급자를 공개 주소에 둔다. Tailscale은 operator 토큰을 검증하려고 이 주소의 discovery 문서와 공개키를 읽는다. GKE · EKS는 자체 공개 발급자가 있어 여기에 넣지 않는다.

| 공개되는 것 | 키 |
|---|---|
| discovery 문서 | `<env>/.well-known/openid-configuration` |
| 공개키(JWKS) | `<env>/openid/v1/jwks` |

버킷의 나머지는 공개되지 않는다. 비TLS 접근은 거부된다. 시험(`terraform test`)이 공개 정책에 이 두 객체만 있는지 고정한다.

- **발급자 주소:** `https://<bucket>.s3.<region>.amazonaws.com/<env>`(출력 `issuers`). 버킷 이름과 리전은 모든 발급자 주소에 들어가므로 한 번 게시하면 바꾸지 않는다.
- **공개키 원본:** `issuers/<env>/jwks.json`(Git). 그 클러스터의 `kubectl get --raw /openid/v1/jwks` 출력이다. 바꾸는 방법은 PR뿐이고, 쓰는 주체는 이 스택의 적용자뿐이다. 비밀 멤버(`d`, `p`, `q` 등)가 있으면 precondition이 적용을 막는다.
- **클러스터 쪽:** kube-apiserver의 `service-account-issuer`를 이 주소로 첫 번째에 두고, 기존 내부 발급자는 두 번째로 남긴다. `service-account-jwks-uri`도 이 주소의 JWKS로 한다. 노드 설정 파일로 코드화한다.
- **키 교체:** 클러스터 서명 키를 바꾸면 새 키를 JWKS에 **더한** PR을 먼저 적용한다. 옛 토큰이 만료된 뒤에 옛 키를 뺀다.
- **노드 재생성:** RKE2 서버 노드를 다시 만들면 서명 키가 바뀐다. 새 클러스터의 JWKS를 다시 게시해야 그 operator가 인증된다.

```sh
cd infra/envs/oidc-issuer
terraform init -backend-config=<비공개 backend 파일>   # 기존 state 버킷, 키 oidc-issuer/terraform.tfstate
terraform plan -var-file=<비공개 tfvars: account_id, region>
```

플랫폼 AWS 계정에만 적용한다(`allowed_account_ids`).
