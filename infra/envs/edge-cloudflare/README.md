# T17 Edge: Cloudflare 구성을 코드로

Cloudflare 쪽 Edge 전부를 이 스택 하나가 갖는다. 지금까지 브라우저 스크립트로 만들고 고친 자원이다.

| 자원 | 값 | 바꾸는 쪽 |
|---|---|---|
| Tunnel ingress(출발지·목적지) | 각자 호스트 → 그 환경 `demo-chat:8080`, 목적지에만 로그인 호스트의 `/realms/bravo/`·`/resources/` → `target-login:8080`, 마지막 `http_status:404` | 이 스택 |
| DNS | Tunnel 호스트마다 proxied CNAME → `<tunnel>.cfargotunnel.com` | 이 스택 |
| 모니터 · pool 2개 · LB | `/readyz` 200, pool당 Tunnel origin 하나, random · affinity 없음 · pool 간 재시도 없음 · 출발지 fallback. 새 LB는 꺼진 채 만든다 | 이 스택 |
| LB 가중치 | 새 LB만 출발지 1 · 목적지 0으로 시작 | [edge-control](../../../services/edge-control/README.md) 하나(`ignore_changes`) |
| LB 활성 | 새 LB만 꺼짐으로 시작 | 실행 담당이 두 origin을 확인한 뒤 켠다(`ignore_changes`). edge-control은 켜지 않는다 |
| Tunnel 자체 · 토큰 | 이 스택 밖 | [edge-tunnels](../edge-tunnels/README.md) |

가중치는 Terraform이 되돌리지 않는다. edge-control은 단일 writer와 자기 journal로 10→50→100→0을 진행하므로 두 번째 writer가 있으면 안 된다.
LB의 다른 설정(steering, fallback, pool 목록, `failover_across_pools`)은 edge-control이 실행 전에 확인하는 조건과 같고 시험이 고정한다.

## 목적지 로그인 주소 (T47)

`hostnames.login`을 넣으면 목적지 Tunnel에 Keycloak 경로가, zone에 그 호스트의 CNAME이 생긴다. **LB를 거치지 않는다.**
Keycloak은 목적지에만 있고, 발급자 주소(`https://<login>/realms/bravo`)는 가중치가 움직이는 동안에도 같아야 한다.

**공개 주소로는 `bravo` realm과 테마 파일만 나간다.** 로그인 호스트의 규칙은 `path = "^/(realms/bravo|resources)/"` 하나이고,
나머지(`/admin/`, `/realms/master/` 등)는 마지막 `http_status:404`로 떨어진다. 같은 8080에 관리 화면과 master realm이 있고,
master realm은 잠금 보호가 꺼져 있으며 관리자는 모든 작성자를 사칭할 수 있기 때문이다. 관리 작업은
`kubectl -n target-dev port-forward svc/target-login 18080:8080`으로만 한다(`target-workloads/README.md`).
Keycloak의 NetworkPolicy는 `app=bravo-edge-tunnel`의 8080만 받으므로, 클러스터 밖에서는 이 Tunnel 규칙이 유일한 입구다.

로그인 흐름 전체가 이 두 접두사만 쓴다. 2026-11-07에 #652(`b6f62ce2`)가 렌더한 Keycloak 26.0.7 이미지와 realm을 컨테이너로 띄워
로그인 화면 → 임시 비밀번호 로그인 → 새 비밀번호 설정 → 콜백(코드 발급)까지 진행하고, 화면 · CSS · JS가 참조하는 경로와
token · certs · logout 엔드포인트를 모았다. 모두 `/realms/bravo/` 또는 `/resources/` 아래였다(`/js/` 등 다른 경로 없음).

1. 목적지에 `identity.py`를 적용하고 `target-login`이 Ready인지 본다. 그 전에 열면 로그인 호스트가 502다.
2. `hostnames.login`을 넣고 plan → apply. 바뀌는 것은 목적지 Tunnel ingress 1건 수정, DNS 1건 추가뿐이어야 한다.
3. 공개 주소에서 열린 경로와 닫힌 경로를 탐침한다. 기대값과 다르면 `hostnames.login`을 빼고 다시 apply해 닫는다.

   ```sh
   L=https://<로그인 호스트>
   curl -s -o /dev/null -w '%{http_code}\n' "$L/realms/bravo/.well-known/openid-configuration"   # 200
   curl -s -o /dev/null -w '%{http_code}\n' "$L/admin/master/console/"                          # 404
   curl -s -o /dev/null -w '%{http_code}\n' "$L/realms/master/.well-known/openid-configuration"  # 404
   ```

4. `terraform output login_issuer`를 목적지 demo-chat의 `AUTH_ISSUER`로 넣는다.

## 적용 (운영자)

토큰은 환경 변수로만 넣는다. 권한: 계정 `Cloudflare Tunnel` 편집, `Load Balancing: Monitors and Pools` 편집, zone `DNS`·`Load Balancers` 편집.
edge-control의 실행 토큰(zone LB 읽기·쓰기만)과 따로 둔다.
계정 `Cloudflare Tunnel` 편집은 API로 Tunnel 토큰까지 읽을 수 있는 강한 권한이다. 이 계정과 이 zone으로만 한정하고, 만료를 짧게(하루 이내) 두며,
가져오기와 첫 apply가 끝나면 폐기한다.

state에는 계정 · zone · Tunnel ID가 들어간다. 기존 암호화 S3 backend에 이 스택만의 key(예: `edge/edge-cloudflare/terraform.tfstate`)를 쓰고,
다른 스택의 key로 init하지 않는다. backend 설정(버킷, 리전, key, `encrypt=true`, KMS 키, `use_lockfile=true`) · tfvars · plan은
`$PRIVATE_DIR`(저장소 밖 개인 디렉터리)에 둔다.

```sh
cd infra/envs/edge-cloudflare
terraform init -backend-config="$PRIVATE_DIR/edge-cloudflare.backend.hcl"
read -rs CLOUDFLARE_API_TOKEN && export CLOUDFLARE_API_TOKEN
terraform plan -var-file="$PRIVATE_DIR/edge-cloudflare.tfvars" -out="$PRIVATE_DIR/edge-cloudflare.tfplan"
terraform apply "$PRIVATE_DIR/edge-cloudflare.tfplan"
unset CLOUDFLARE_API_TOKEN
```

Cloudflare LB · pool · 모니터는 태그를 지원하지 않는다. 그래서 `component` 태그 대신 이름 · 설명의 `bravo-` · `Team Bravo T17`로
이 스택의 자원임을 구분한다(DNS 레코드는 `comment`). 비용 정산 대상은 zone의 Load Balancing 요금 하나다.

**처음 한 번은 가져오기다.** 이미 있는 Tunnel 설정 · DNS 2건 · 모니터 · pool 2개 · LB의 ID를 비공개 tfvars의 `adopt`에 넣으면
plan이 `import`만 보여야 한다. 수정이 보이면 코드와 실제가 다르다는 뜻이므로 apply하지 않고 코드나 실제 중 맞는 쪽을 정한다.
가져온 뒤에는 `adopt`를 지운다. ID · 호스트 이름 · 계정은 공개 증빙에 붙이지 않는다([공개 증빙 절차](../../../docs/public-evidence-policy.md)).

## 검증

- `terraform test` 5개(모의 공급자): 로그인은 선언 전엔 닫힘 · 목적지 Keycloak으로만 · 출발지엔 없음 · DNS는 Tunnel 직결,
  로그인 호스트 규칙은 path가 있는 하나뿐(path를 빼면 실패함을 확인), 두 Tunnel 모두 404로 끝남, LB가 edge-control 조건과 같고
  꺼진 채 시작, 로그인 호스트를 가중치 주소와 같게 두면 거부
- 실제 구성 대조(2026-11-07 18:09 KST, 기존 대시보드 세션, GET만): 두 Tunnel ingress · DNS 2건 · 모니터 · pool 2개 · LB가
  이 코드의 값과 같았다. 다른 것은 Tunnel 설정의 `warp-routing: {enabled: false}`뿐인데, API 기본값이고 공급자 5.x 스키마에 없는 키다.
  LB는 활성, 로그인 호스트 레코드는 아직 없다. 원본은 비공개 운영 원장에 있다.
- 실제 계정 `terraform plan` · `apply`는 아직이다. 이 스택용 편집 토큰이 없다(위 권한). 그래서 "가져오기만 나온다"는 plan으로는 아직 확인하지 않았다.
