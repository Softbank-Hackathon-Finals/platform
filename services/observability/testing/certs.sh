# 시험 전용 CA와 인증서. 운영 발급 스크립트(deploy/certs/issue.sh)를 그대로 써서 그 스크립트도 함께 검증한다.
# run.sh · k8s.sh · rotation.sh가 source한다. CERT_DIR이 정해져 있어야 하고, 끝나면 호출한 쪽이 지운다.
ISSUE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/certs" && pwd)/issue.sh"
ca() { "$ISSUE" ca "$CERT_DIR/$1" 2 >/dev/null; cp "$CERT_DIR/$1/ca.crt" "$CERT_DIR/$1.crt"; }
leaf() { # 디렉터리 CA SAN [이름(기본: 디렉터리)]
  "$ISSUE" leaf "$CERT_DIR/$2" "${4:-$1}" "$3" "$CERT_DIR/$1" 1 >/dev/null
}
# 컨테이너 사용자(nobody · 10001)가 마운트한 시험용 키를 읽도록 연다. 운영 Secret은 0440 + fsGroup으로 읽는다
open_for_containers() { chmod -R a+rX "$CERT_DIR"; }
