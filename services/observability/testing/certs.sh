# 시험 전용 CA와 인증서 생성. run.sh · k8s.sh가 source한다. CERT_DIR이 정해져 있어야 한다.
# 컨테이너 사용자(nobody · 10001)가 읽도록 권한을 연다. 시험이 끝나면 호출한 쪽이 CERT_DIR을 지운다.
ca() { openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 -subj "/CN=$1" \
  -keyout "$CERT_DIR/$1.key" -out "$CERT_DIR/$1.crt" 2>/dev/null; }
leaf() { # 이름 CA SAN
  mkdir -p "$CERT_DIR/$1"
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$1" \
    -keyout "$CERT_DIR/$1/tls.key" -out "$CERT_DIR/$1/tls.csr" 2>/dev/null
  openssl x509 -req -days 1 -in "$CERT_DIR/$1/tls.csr" -CA "$CERT_DIR/$2.crt" -CAkey "$CERT_DIR/$2.key" \
    -CAcreateserial -out "$CERT_DIR/$1/tls.crt" \
    -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth,clientAuth\n' "$3") 2>/dev/null
  cp "$CERT_DIR/$2.crt" "$CERT_DIR/$1/ca.crt"
}
