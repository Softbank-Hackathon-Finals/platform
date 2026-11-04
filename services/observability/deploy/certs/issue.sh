#!/usr/bin/env bash
# T37 관측 경로 인증서 발급. 절차와 이름 규칙: docs/observability/certificates.md
#
#   issue.sh ca   <출력 디렉터리> [유효일=90]
#   issue.sh leaf <CA 디렉터리> <이름> <SAN> <출력 디렉터리> [유효일=30]
#   issue.sh check <인증서 파일> [남은 일수 하한=10]
#
# 키는 0600으로 쓰고 화면에 출력하지 않는다. CA 키는 발급할 때만 꺼내고 클러스터 Secret에 넣지 않는다.
# CA 이름은 매번 고유하고, leaf에는 발급자 키 식별자(AKID)를 넣는다. CA 교체 중 두 CA를 함께 믿을 때 체인을 헷갈리지 않게 한다.
# 출력 디렉터리의 ca.crt · tls.crt · tls.key가 그대로 쿠버네티스 Secret의 키 이름이 된다.
set -euo pipefail
umask 077

die() { echo "issue.sh: $*" >&2; exit 2; }

ca() {
  local out=$1 days=${2:-90}
  [ -e "$out/ca.key" ] && die "${out}/ca.key가 이미 있다(덮어쓰지 않는다)"
  mkdir -p "$out"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days "$days" \
    -subj "/CN=bravo-observability-ca-$(date -u +%Y%m%d)-$(openssl rand -hex 4)" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" \
    -keyout "$out/ca.key" -out "$out/ca.crt" 2>/dev/null
  chmod 0644 "$out/ca.crt"
  echo "CA: $out/ca.crt ($(openssl x509 -in "$out/ca.crt" -noout -enddate))"
}

leaf() {
  local cadir=$1 name=$2 san=$3 out=$4 days=${5:-30}
  [ -f "$cadir/ca.key" ] || die "${cadir}/ca.key가 없다"
  # leaf가 CA보다 오래 살면 CA 만료와 함께 못 쓰게 된다
  openssl x509 -in "$cadir/ca.crt" -noout -checkend $((days * 86400)) >/dev/null || die "CA가 ${days}일 안에 만료된다. 유효일을 줄이거나 CA를 먼저 교체한다"
  [[ $name =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "이름은 소문자 · 숫자 · -만: $name"
  # 허용 이름(client_allowed_sans)과 대조되는 DNS 이름이 반드시 SAN에 있어야 한다
  case ",$san," in *",DNS:$name,"*) ;; *) die "SAN에 DNS:${name}이 있어야 한다: ${san}";; esac
  mkdir -p "$out"
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$name" \
    -keyout "$out/tls.key" -out "$out/tls.csr" 2>/dev/null
  openssl x509 -req -days "$days" -in "$out/tls.csr" -CA "$cadir/ca.crt" -CAkey "$cadir/ca.key" \
    -CAcreateserial -out "$out/tls.crt" \
    -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth,clientAuth\nkeyUsage=critical,digitalSignature\nbasicConstraints=critical,CA:FALSE\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n' "$san") 2>/dev/null
  cp "$cadir/ca.crt" "$out/ca.crt"
  shred -u "$out/tls.csr" 2>/dev/null || unlink "$out/tls.csr"
  chmod 0644 "$out/tls.crt" "$out/ca.crt"
  chmod 0600 "$out/tls.key"
  echo "${name}: ${out}/tls.crt (SAN ${san}, $(openssl x509 -in "$out/tls.crt" -noout -enddate))"
}

check() {
  local crt=$1 min_days=${2:-10}
  if openssl x509 -in "$crt" -noout -checkend $((min_days * 86400)) >/dev/null; then
    echo "OK $crt ($(openssl x509 -in "$crt" -noout -enddate))"
  else
    echo "교체 필요 ${crt}: ${min_days}일 안에 만료 ($(openssl x509 -in "$crt" -noout -enddate))"
    return 1
  fi
}

cmd=${1:-}
shift || true
case $cmd in
  ca) [ $# -ge 1 ] || die "사용: issue.sh ca <출력> [유효일]"; ca "$@" ;;
  leaf) [ $# -ge 4 ] || die "사용: issue.sh leaf <CA> <이름> <SAN> <출력> [유효일]"; leaf "$@" ;;
  check) [ $# -ge 1 ] || die "사용: issue.sh check <인증서> [일수]"; check "$@" ;;
  *) die "명령: ca | leaf | check" ;;
esac
