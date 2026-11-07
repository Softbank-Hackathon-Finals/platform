# 공개 입구 조정기 (public entry)

Git에 선언한 공개 호스트를 Cloudflare에 맞춘다. 사람이 대시보드나 API를 손으로 고치지 않는다.

| 맞추는 것 | 기준 |
|---|---|
| Tunnel 하나(`tunnel`) | 없으면 만든다. 토큰은 `token_secret`의 Secret에 쓴다(값이 같으면 쓰지 않는다) |
| Tunnel 경로 | 선언한 호스트 → 클러스터 안 Service. 모든 경로가 origin에서도 Access 토큰을 요구한다. 마지막은 404 |
| DNS | 호스트마다 proxied CNAME → `<tunnel>.cfargotunnel.com`. 주석이 `managed-by: bravo-public-entry`인 레코드만 바꾸고 지운다 |
| Access | self-hosted 앱 하나(호스트 전부)와 재사용 정책 하나(허용 이메일) |

- 선언: `infra/envs/public-entry/entries.json`(플랫폼 Argo가 ConfigMap으로 배치). 호스트는 zone 바로 아래 한 단계만(Cloudflare 기본 인증서 범위), 서비스는 `http(s)://<svc>.<ns>.svc:<port>`만 받는다.
- 자격: 플랫폼 OpenBao `platform/edge/cloudflare-gitops`(`token`, `account_id`, `zone_id`, `access_emails`)를 ESO가 파일로 넣는다. 이메일은 Git에 두지 않는다.
- 남의 레코드가 선언한 호스트에 있으면 DNS를 쓰지 않고 멈춘다. 오류는 다음 주기(기본 60초)에 다시 시도하고, 자격 값은 출력하지 않는다.
- 고객 클러스터는 Cloudflare 자격도 인터넷 입구도 갖지 않는다. 고객 화면은 tailnet으로 플랫폼에 닿고, 플랫폼 Tunnel 하나가 중계한다.

```sh
cd services/public-entry && uv sync --locked && uv run --locked pytest
```
