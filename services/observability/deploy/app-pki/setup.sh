#!/usr/bin/env bash
# T37 앱 → Collector mTLS의 앱 CA를 목적지 OpenBao PKI에 둔다(#104 할 일 7). CA 키는 OpenBao 밖으로 나오지 않는다.
# 사람이 CA 키를 보관하거나 인증서 Secret을 kubectl로 만들지 않는다. 앱 쪽은 ESO 생성기, Collector 쪽은 사이드카가 발급받는다.
#
#   setup.sh <kube-context> <기대 kube-system UID>     # 운영자 토큰은 stdin 한 줄(argv · 환경변수 · 파일에 두지 않는다)
#
# 이미 있는 마운트 · CA는 그대로 두고, 역할 · 정책 · 인증 역할은 같은 값으로 다시 쓴다(여러 번 실행해도 같다).
set -euo pipefail
ctx=${1:?kube context} uid=${2:?expected kube-system UID}
[ "$(kubectl --context "$ctx" get ns kube-system -o jsonpath='{.metadata.uid}')" = "$uid" ] || { echo 'cluster identity mismatch' >&2; exit 1; }
exec kubectl --context "$ctx" -n bravo-secrets exec -i target-openbao-0 -c openbao -- sh -ec '
read -r BAO_TOKEN; export BAO_TOKEN
m=pki-telemetry
bao secrets list -format=json | grep -q "\"$m/\"" || bao secrets enable -path=$m -max-lease-ttl=2160h pki >/dev/null
bao list -format=json $m/issuers >/dev/null 2>&1 || bao write -field=issuer_id $m/root/generate/internal \
  common_name="bravo-app-telemetry-ca-$(date -u +%Y%m%d)" key_type=ec key_bits=256 ttl=2160h >/dev/null
# 앱 클라이언트(demo-chat) 전용 · Collector 수신 서버 전용. 서로의 용도로 쓸 수 없다. 발급 기록을 저장하지 않는다(no_store)
bao write $m/roles/app-metrics-client allowed_domains=demo-chat.target-dev allow_bare_domains=true allow_subdomains=false \
  client_flag=true server_flag=false key_type=ec key_bits=256 ttl=72h max_ttl=168h no_store=true >/dev/null
bao write $m/roles/collector-receiver allowed_domains=bravo-collector.bravo-observability.svc allow_bare_domains=true allow_subdomains=false \
  client_flag=false server_flag=true key_type=ec key_bits=256 ttl=72h max_ttl=168h no_store=true >/dev/null
printf "path \"$m/issue/app-metrics-client\" { capabilities = [\"update\"] }\n" | bao policy write telemetry-app-client - >/dev/null
printf "path \"$m/issue/collector-receiver\" { capabilities = [\"update\"] }\n" | bao policy write telemetry-collector-receiver - >/dev/null
bao write auth/kubernetes/role/telemetry-app-client bound_service_account_names=bravo-metrics-issuer \
  bound_service_account_namespaces=target-dev audience=openbao token_policies=telemetry-app-client token_ttl=300 token_max_ttl=900 >/dev/null
bao write auth/kubernetes/role/telemetry-collector-receiver bound_service_account_names=bravo-collector \
  bound_service_account_namespaces=bravo-observability audience=openbao token_policies=telemetry-collector-receiver token_ttl=300 token_max_ttl=900 >/dev/null
bao read -field=issuer_name $m/issuer/default 2>/dev/null || true
bao read -field=certificate $m/cert/ca | grep -c "BEGIN CERTIFICATE"
'
