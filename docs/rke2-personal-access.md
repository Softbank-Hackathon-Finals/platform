# T1 · RKE2 개인 개발 접근 인수

이 문서는 기존 T7 비공개 RKE2에 **본인 SSO → SSM 터널 → 본인 Kubernetes 인증서**로 접속하는 운영 절차다. 승인된 사람 개발자에게 개인별 `cluster-admin`을 부여하며, AI 워커·CI·서비스계정 권한으로 전파하지 않는다. 문서의 명령은 검토용 예시이며 실행 완료 기록이 아니다. 실제 개인 로그인·발급·인수 결과는 별도 영수증에 남긴다.

[접근 계약](access-readiness.md), [T7 접속 경계](../infra/envs/platform-aws/README.md#bootstrap-and-operator-access), [담당 범위 규약](development.md#담당-태스크를-넘는-변경)을 따른다. 기존 접근 계획의 제한 역할 시험과 별도로, 이 절차는 후속 승인된 사람용 개발 관리자 범위를 다룬다. [T10 콘솔 운영자 계약](contracts/T10-operator-access-v1-proposal.md)의 로그인·계획별 승인 자격을 발급하는 절차는 아니다.

AWS는 터널을 연 SSO 주체를 확인하고 RKE2는 인증서의 CN을 사용자로 확인한다. 둘을 자동 연결하는 인증 서버는 없다. 운영자가 **개인 디렉터리 신원·계정 할당·CSR 공개키·고유 CN**을 대조해야 한다. 같은 permission-set 역할을 쓴다는 사실이나 STS 세션 이름만으로 특정 팀원임을 증명하지 않는다.

## 1. 실행 전 입력과 소유 확인

- 운영자 본인의 [SSO 검증](operator-sso-verification.md)이 먼저 성공해야 한다. 이후 대상 팀원이 자기 장치에서 로그인·필요한 본인 인증을 완료한다. 다른 사람의 로그인이나 기존 IAM 자격으로 대신하지 않는다.
- 대상은 기존 T7 호스트 하나다. 증설·재시작·다른 세션의 작업이 진행 중이면 소유자의 종료·SSM/API 정상 readback을 먼저 확인한다. 이 절차는 RKE2 재설치, API 공개, SG 변경, 새 인증 서비스 설치를 포함하지 않는다.
- 운영자는 현재 API 버전, `kubernetes.io/kube-apiserver-client` signer 구성, 신뢰하는 client CA, 공개 server CA와 서버 인증서 SAN을 확인한다. 준비되지 않은 signer를 우회해 CA 개인 키를 복사하거나 직접 서명하지 않는다.
- 본인용과 운영자용 작업은 별도 터미널·비공개 디렉터리에서 진행한다. Bash 예시는 `set -euo pipefail`, `umask 077`을 사용하고 명령 추적(`set -x`)을 끈다. 실제 계정·ARN·이름·CSR·인증서·kubeconfig·원문 출력은 Git과 공개 댓글에 넣지 않는다.

검토한 비공개 입력은 다음과 같다. 리전에는 기본값을 두지 않는다.

| 입력 | 확인할 의미 |
|---|---|
| `BRAVO_EXPECTED_AWS_ACCOUNT`, `RKE2_PROFILE`, `RKE2_REGION`, `RKE2_INSTANCE_ID` | 기대 계정, **본인** SSO 프로필, 기존 호스트의 리전·ID. 현재 STS 값을 그대로 기대 계정으로 복사하지 않는다. |
| 개인 매핑 | 기존 로스터의 Identity Store UserId, 계정/permission-set 할당, 확인된 본인 전달 경로. SSO 토큰·암호는 수집하지 않는다. |
| `RKE2_MEMBER_ALIAS` | 개인정보 없는 운영 별칭. 예: `member-01`; 영문 소문자로 시작하는 1~20자의 소문자·숫자·하이픈. |
| `RKE2_TLS_NAME` | 현재 서버 인증서 SAN에서 확인한 이름 또는 IP. 추측한 hostname이나 TLS 검증 생략을 쓰지 않는다. |
| 공개 server CA·지문 | 해당 클러스터의 서버 검증용 공개 CA. 운영자의 인증된 경로로 받아 지문을 대조한다. client CA와 역할을 혼동하지 않는다. |
| 수명·회수 담당 | 발급마다 최대 8시간, 종료 시각, 일시 회수 시험·복구 여부, 실제 퇴장 처리 담당. |

## 2. 본인 장치: SSO 확인과 개인 키·CSR

AWS CLI v2, Session Manager plugin, 서버 버전과 호환되는 kubectl, OpenSSL을 사용한다. 아래 환경 초기화는 이후 개인 키·kubeconfig·kubectl 작업을 이어갈 **본인 작업 터미널 A**에서 수행한다. 프로필의 임의 endpoint override를 사용하지 않으며, 검증 도구가 혼합 인증 설정을 거부하면 먼저 그 설정을 해결한다.

```bash
set -euo pipefail
set +x
umask 077
: "${RKE2_PROFILE:?본인 SSO 프로필}" "${RKE2_REGION:?검토한 호스트 리전}"
: "${BRAVO_EXPECTED_AWS_ACCOUNT:?검토한 기대 계정}"
export BRAVO_EXPECTED_AWS_ACCOUNT
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
unset AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE
unset AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_CREDENTIALS_FULL_URI
export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true AWS_EC2_METADATA_DISABLED=true
aws sso login --profile "$RKE2_PROFILE"
# platform checkout에서 실행. exit 0과 SSO_SESSION_OBSERVED를 모두 확인한다.
python3 scripts/verify_operator_sso.py --profile "$RKE2_PROFILE"
```

이 결과는 기대 SSO 역할의 현재 STS 세션만 확인한다. 운영자는 기존 로스터·Identity Store 사용자 및 계정 할당 readback과 본인이 확인한 전달 경로를 대조한다. 이름이 닮은 IAM 사용자, 같은 역할의 다른 사용자, 타인의 프로필을 개인 매핑으로 채택하지 않는다. 운영자 검증이 팀원 검증을 대신하지 않는다.

다음은 **접속할 본인이 자기 장치에서만** 실행한다. 운영자가 팀원 대신 키를 만들거나 자신의 키를 나눠주지 않는다. 키는 파일 권한으로 보호하며 이메일·채팅·Git·공유 드라이브·호스트로 보내지 않는다. 재발급마다 새 키와 새 고유 CN을 사용한다.

```bash
: "${RKE2_MEMBER_ALIAS:?검토한 개인 별칭}"
[[ "$RKE2_MEMBER_ALIAS" =~ ^[a-z][a-z0-9-]{0,19}$ ]]
RKE2_ISSUANCE_ID=$(openssl rand -hex 8)
RKE2_CSR_NAME="human-${RKE2_MEMBER_ALIAS}-${RKE2_ISSUANCE_ID}"
RKE2_USER_CN="bravo:human:${RKE2_MEMBER_ALIAS}:${RKE2_ISSUANCE_ID}"
RKE2_PERSONAL_DIR="$HOME/.bravo/rke2/$RKE2_CSR_NAME"
mkdir -p "$HOME/.bravo/rke2"
mkdir -m 700 "$RKE2_PERSONAL_DIR"  # 기존 경로면 중단하며 덮어쓰지 않는다.
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out "$RKE2_PERSONAL_DIR/client.key"
chmod 600 "$RKE2_PERSONAL_DIR/client.key"
openssl req -new -sha256 -key "$RKE2_PERSONAL_DIR/client.key" \
  -subj "/CN=$RKE2_USER_CN" -out "$RKE2_PERSONAL_DIR/client.csr"
openssl req -in "$RKE2_PERSONAL_DIR/client.csr" -noout -verify -subject
openssl req -in "$RKE2_PERSONAL_DIR/client.csr" -pubkey -noout |
  openssl pkey -pubin -outform DER | openssl dgst -sha256
```

본인은 **CSR 파일·CSR 이름·CN·공개키 SHA-256**만 승인된 개인 전달 경로로 보낸다. 운영자는 해당 사람에게 지문과 요청을 확인받아 개인 매핑 기록에 결속한다. 공유 permission-set 역할과 CSR의 CN 사이에는 자동 증명이 없으므로 이 확인을 생략하지 않는다.

## 3. 운영자: CSR 검토·발급과 공개 자료 전달

이 절의 `kadmin`은 **기존 RKE2 호스트의 운영자 관리 세션 안에서만** 사용한다. 호스트의 관리자 kubeconfig는 그 자리에 두며 복사·출력하지 않는다. 운영자의 관리 세션은 CSR 승인·RBAC 설정용이며 개인 접속 성공의 증거가 아니다. `RKE2_REVIEW_DIR`는 이번 발급 전용으로 만든 권한 `0700`의 새 디렉터리여야 한다.

```bash
set -euo pipefail
set +x
umask 077
kadmin() {
  sudo /var/lib/rancher/rke2/bin/kubectl \
    --kubeconfig=/etc/rancher/rke2/rke2.yaml "$@"
}
: "${RKE2_REVIEW_DIR:?호스트의 새 비공개 검토 디렉터리}"
: "${RKE2_MEMBER_ALIAS:?확인한 개인 별칭}" "${RKE2_ISSUANCE_ID:?확인한 발급 ID}"
[[ "$RKE2_MEMBER_ALIAS" =~ ^[a-z][a-z0-9-]{0,19}$ ]]
[[ "$RKE2_ISSUANCE_ID" =~ ^[0-9a-f]{16}$ ]]
RKE2_CSR_NAME="human-${RKE2_MEMBER_ALIAS}-${RKE2_ISSUANCE_ID}"
RKE2_USER_CN="bravo:human:${RKE2_MEMBER_ALIAS}:${RKE2_ISSUANCE_ID}"
# 받은 client.csr만 이 디렉터리에 둔다. client.key는 받지 않는다.
openssl req -in "$RKE2_REVIEW_DIR/client.csr" -noout -verify -text
openssl req -in "$RKE2_REVIEW_DIR/client.csr" -pubkey -noout |
  openssl pkey -pubin -outform DER | openssl dgst -sha256
```

**등록 전에** 출력의 서명 유효성, CN이 `RKE2_USER_CN`과 정확히 같은지, 본인이 확인한 공개키 지문을 비교한다. CN 하나 외의 subject 항목·조직(O)·요청 extension이 있으면 이 간단한 발급 절차에서 수락하지 않고 본인에게 새 CSR을 요청한다. `system:masters`, `system:nodes`, 다른 사람 CN은 허용하지 않는다. CSR 안의 요청을 읽어야 하며 파일명이나 Kubernetes CSR의 `spec.username`만 확인해서는 안 된다. 운영자가 등록하면 `spec.username`은 운영자 신원일 수 있다.

검토를 통과한 **PEM CSR에서 운영자가** Kubernetes 요청을 만든다. 전달받은 임의 YAML을 그대로 적용하지 않는다. 아래는 최대 8시간 요청이며, 첫 만료 실측에는 별도의 고유 발급 ID로 `600`초를 선택할 수 있다. 기존 객체를 덮어쓰지 않도록 `create`를 사용한다.

```bash
RKE2_TTL_SECONDS=28800
[[ "$RKE2_TTL_SECONDS" =~ ^[0-9]+$ ]]
(( RKE2_TTL_SECONDS >= 600 && RKE2_TTL_SECONDS <= 28800 ))
RKE2_REQUEST_B64=$(openssl base64 -A -in "$RKE2_REVIEW_DIR/client.csr")
cat > "$RKE2_REVIEW_DIR/request.yaml" <<EOF
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: $RKE2_CSR_NAME
spec:
  request: $RKE2_REQUEST_B64
  signerName: kubernetes.io/kube-apiserver-client
  expirationSeconds: $RKE2_TTL_SECONDS
  usages:
    - digital signature
    - key encipherment
    - client auth
EOF
kadmin create -f "$RKE2_REVIEW_DIR/request.yaml"
kadmin get csr "$RKE2_CSR_NAME" -o yaml > "$RKE2_REVIEW_DIR/submitted-csr.yaml"
```

등록된 UID·요청·signer·수명을 원래 검토본과 대조한 뒤 같은 객체만 승인한다. 충돌·삭제/교체·다른 승인 상태가 있으면 재사용하지 않고 담당자가 원인을 확인한다. 기본 client signer는 자동 승인하지 않는다. `Approved`는 발급 완료가 아니며 `status.certificate`가 비어 있으면 중단하고 제어 평면의 서명 상태를 확인한다. [Kubernetes 1.35 CSR 계약](https://v1-35.docs.kubernetes.io/docs/reference/access-authn-authz/certificate-signing-requests/)

```bash
kadmin certificate approve "$RKE2_CSR_NAME"
kadmin get csr "$RKE2_CSR_NAME" -o jsonpath='{.status.certificate}' |
  openssl base64 -d -A > "$RKE2_REVIEW_DIR/client.crt"
test -s "$RKE2_REVIEW_DIR/client.crt"
openssl x509 -in "$RKE2_REVIEW_DIR/client.crt" \
  -noout -subject -issuer -dates -fingerprint -sha256
openssl x509 -in "$RKE2_REVIEW_DIR/client.crt" -pubkey -noout |
  openssl pkey -pubin -outform DER | openssl dgst -sha256
sudo openssl verify -purpose sslclient \
  -CAfile /var/lib/rancher/rke2/server/tls/client-ca.crt \
  "$RKE2_REVIEW_DIR/client.crt"
sudo cat /var/lib/rancher/rke2/server/tls/server-ca.crt > "$RKE2_REVIEW_DIR/server-ca.crt"
openssl dgst -sha256 "$RKE2_REVIEW_DIR/server-ca.crt"
```

발급된 인증서의 CN·공개키가 검토본과 같고 client authentication 용도인지, 아직 유효하며 **실제 notAfter가 발급 승인 시각부터 8시간 이내인지** 확인한다. signer의 상한에 따라 요청보다 짧아질 수 있다. 요청 숫자만으로 실제 만료를 기록하지 않는다. 위 검사 실패 시 RBAC를 부여하지 않는다. CA 교체 중이면 현재 신뢰 체인·server CA bundle을 담당자와 확인하며 새 CA를 임의 생성하지 않는다.

본인에게 보내는 것은 `client.crt`, 공개 `server-ca.crt`, 확인한 `RKE2_TLS_NAME`, 인스턴스·리전·CSR 이름·CN·만료·회수 담당 정보다. 본인의 키를 포함한 kubeconfig 완성본을 운영자가 만들거나 전송하지 않는다. 공개 자료도 운영 식별자를 포함하므로 승인된 비공개 경로로 전달한다. 수신자는 인증된 경로에서 확인한 **server CA bundle 전체의 SHA-256**·공개키 지문과 파일의 대상을 대조한다. CA bundle의 첫 인증서만 확인하고 나머지를 신뢰하지 않는다.

## 4. 본인 장치: 개인 kubeconfig와 엄격한 TLS 터널

본인은 받은 인증서·CA를 자신의 기존 `RKE2_PERSONAL_DIR`에 저장하고 **터미널 A**에서 아래를 실행한다. 다른 터미널에는 셸 변수가 자동 전달되지 않는다. A를 새로 열었다면 2절에서 사용한 개인 디렉터리·CSR 이름·CN과 전달받은 `RKE2_TLS_NAME`을 비공개 기록에서 다시 설정한다. 키·CSR 생성은 반복하지 않는다. CN·만료를 다시 읽고, 다음 두 공개키 지문이 같은지 확인한다. 개인 키 자체를 출력하지 않는다.

```bash
: "${RKE2_PERSONAL_DIR:?본인의 기존 발급 디렉터리}" "${RKE2_USER_CN:?이번 발급의 CN}"
: "${RKE2_CSR_NAME:?이번 발급의 CSR 이름}" "${RKE2_TLS_NAME:?인증된 경로로 대조한 서버 SAN}"
openssl pkey -in "$RKE2_PERSONAL_DIR/client.key" -pubout -outform DER |
  openssl dgst -sha256
openssl x509 -in "$RKE2_PERSONAL_DIR/client.crt" -pubkey -noout |
  openssl pkey -pubin -outform DER | openssl dgst -sha256
openssl x509 -in "$RKE2_PERSONAL_DIR/client.crt" -noout -subject -dates
openssl dgst -sha256 "$RKE2_PERSONAL_DIR/server-ca.crt"
RKE2_LOCAL_PORT=16443
RKE2_KUBECONFIG="$RKE2_PERSONAL_DIR/kubeconfig"
test ! -e "$RKE2_KUBECONFIG"
kubectl --kubeconfig="$RKE2_KUBECONFIG" config set-cluster bravo-rke2 \
  --server="https://127.0.0.1:$RKE2_LOCAL_PORT" \
  --certificate-authority="$RKE2_PERSONAL_DIR/server-ca.crt" \
  --embed-certs=true --tls-server-name="$RKE2_TLS_NAME"
kubectl --kubeconfig="$RKE2_KUBECONFIG" config set-credentials "$RKE2_USER_CN" \
  --client-certificate="$RKE2_PERSONAL_DIR/client.crt" \
  --client-key="$RKE2_PERSONAL_DIR/client.key" --embed-certs=false
kubectl --kubeconfig="$RKE2_KUBECONFIG" config set-context personal \
  --cluster=bravo-rke2 --user="$RKE2_USER_CN"
kubectl --kubeconfig="$RKE2_KUBECONFIG" config use-context personal
chmod 600 "$RKE2_KUBECONFIG"
```

기존 기본 kubeconfig와 합치지 않고 매번 명시한 파일을 쓴다. `insecure-skip-tls-verify`, 토큰, exec 인증 plugin, 임의 proxy URL을 추가하지 않는다. `tls-server-name`은 CA 검증에 더해 확인할 서버 이름을 지정한다. 새 kubeconfig의 클러스터·사용자 참조를 비공개로 검토한 뒤 연결한다. [kubeconfig TLS 필드](https://kubernetes.io/docs/reference/config-api/kubeconfig.v1/)

터널은 **별도 터미널 B**에서 연다. B에 검토한 `RKE2_PROFILE`, `RKE2_REGION`, `BRAVO_EXPECTED_AWS_ACCOUNT`를 직접 설정하고 **2절의 첫 환경 초기화·SSO 확인 블록만** 이 터미널에서도 실행한다. 이어 `RKE2_INSTANCE_ID`와 A에서 만든 개인 kubeconfig의 절대경로인 `RKE2_KUBECONFIG`를 설정한다. A의 변수나 AWS 인증 환경이 B에 남아 있다고 가정하지 않는다.

아래에서 B의 포트도 명시하고, 로컬 kubeconfig의 서버 주소와 일치하는지 확인한 뒤 SSM을 시작한다. `config view`는 로컬 파일 조회이며 API 접속이나 키 출력이 아니다. session ID는 비공개로 기록한다. `AWS-StartPortForwardingSession`은 관리 호스트 자신의 포트로 연결하므로 다른 remote-host 경로를 사용할 필요가 없다. 로컬 포트가 이미 사용 중이면 다른 포트를 정하고 **A의 kubeconfig 서버 포트와 B의 포트를 함께** 맞춘다.

```bash
: "${RKE2_PROFILE:?B에서 SSO 확인한 본인 프로필}" "${RKE2_REGION:?B에서 확인한 호스트 리전}"
: "${RKE2_INSTANCE_ID:?검토한 기존 플랫폼 호스트}"
: "${RKE2_KUBECONFIG:?A에서 만든 개인 kubeconfig의 절대경로}"
RKE2_LOCAL_PORT=16443
test -f "$RKE2_KUBECONFIG"
test "$(kubectl --kubeconfig="$RKE2_KUBECONFIG" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')" = "https://127.0.0.1:$RKE2_LOCAL_PORT"
aws ssm start-session --profile "$RKE2_PROFILE" --region "$RKE2_REGION" \
  --target "$RKE2_INSTANCE_ID" --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"6443\"],\"localPortNumber\":[\"$RKE2_LOCAL_PORT\"]}"
```

터널은 루프백에서만 사용할 것을 확인하고, 다른 장치에 중계하지 않는다. **B를 열어 둔 채 A로 돌아와** 아래와 5절의 본인 명령을 실행한다. A도 새 셸이면 기존 `RKE2_PERSONAL_DIR`, `RKE2_CSR_NAME`, `RKE2_USER_CN`, `RKE2_TLS_NAME`과 B에서 선택한 `RKE2_LOCAL_PORT`를 다시 설정한다. 개인 kubeconfig 경로는 기존 디렉터리에서 복원하며, B의 변수·함수가 A로 전달된다고 가정하지 않는다. 루프백 통신에 환경 proxy가 개입하지 않도록 한다. [SSM 포트 전달](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html#sessions-start-port-forwarding)

```bash
: "${RKE2_PERSONAL_DIR:?본인의 기존 발급 디렉터리}" "${RKE2_CSR_NAME:?이번 발급의 CSR 이름}"
: "${RKE2_USER_CN:?이번 발급의 CN}" "${RKE2_TLS_NAME:?확인한 서버 SAN}"
: "${RKE2_LOCAL_PORT:?B에서 선택한 동일 로컬 포트}"
RKE2_KUBECONFIG="$RKE2_PERSONAL_DIR/kubeconfig"
test -f "$RKE2_KUBECONFIG"
test "$(kubectl --kubeconfig="$RKE2_KUBECONFIG" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')" = "https://127.0.0.1:$RKE2_LOCAL_PORT"
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
kself() { kubectl --kubeconfig="$RKE2_KUBECONFIG" --request-timeout=15s "$@"; }
kself auth whoami -o json
```

실제 username이 정확히 `RKE2_USER_CN`이고 예상하지 않은 특권 그룹이 없는지 확인한다. `--as`, 관리자 kubeconfig, 운영자 대신 실행한 결과는 개인 인증 증거로 쓰지 않는다. 아직 binding을 만들지 않았다면 인증은 성공해도 업무 리소스 조회는 거부되는 것이 예상 결과다.

## 5. 운영자와 본인: 무자격·허용·회수 대조

운영자는 이 발급 전용 시험 namespace와 ConfigMap만 만든다. 기존 업무 namespace나 Secret을 대조군으로 사용하지 않는다. 이미 같은 이름이 있으면 멈추며 자신의 시험 객체임을 확인한 UID를 비공개 기록에 남긴다.

```bash
RKE2_TEST_NS="access-${RKE2_CSR_NAME}"
RKE2_BINDING="$RKE2_CSR_NAME"
kadmin create namespace "$RKE2_TEST_NS"
kadmin -n "$RKE2_TEST_NS" create configmap access-canary --from-literal=stage=before-grant
```

본인도 같은 시험 namespace 값을 설정한다. 자격 없는 대조군은 기존 kubeconfig에서 사용자만 바꾼 파일이 아니라, **CA·서버·context만 있는 새 파일**로 만든다. 같은 정상 터널·TLS 조건에서 ConfigMap 조회를 시도한다.

```bash
RKE2_TEST_NS="access-${RKE2_CSR_NAME}"
RKE2_ANON_CONFIG="$RKE2_PERSONAL_DIR/no-credentials.kubeconfig"
test ! -e "$RKE2_ANON_CONFIG"
kubectl --kubeconfig="$RKE2_ANON_CONFIG" config set-cluster bravo-rke2 \
  --server="https://127.0.0.1:$RKE2_LOCAL_PORT" --tls-server-name="$RKE2_TLS_NAME" \
  --certificate-authority="$RKE2_PERSONAL_DIR/server-ca.crt" --embed-certs=true
kubectl --kubeconfig="$RKE2_ANON_CONFIG" config set-context anonymous --cluster=bravo-rke2
kubectl --kubeconfig="$RKE2_ANON_CONFIG" config use-context anonymous
if kubectl --kubeconfig="$RKE2_ANON_CONFIG" --request-timeout=15s \
  -n "$RKE2_TEST_NS" get configmap access-canary > "$RKE2_PERSONAL_DIR/anonymous.log" 2>&1; then
  echo 'FAIL: 무자격 리소스 조회가 허용됨' >&2
  exit 1
fi
```

비공개 오류를 검토해 API의 `Unauthorized` 또는 `Forbidden`인지 확인한다. timeout, 연결 실패, TLS 오류, 클라이언트 설정 오류는 인증·권한 거부의 증거가 아니며 `UNKNOWN`이다. 자격 없는 `/readyz` 응답만으로 접근 통제를 판정하지 않는다.

운영자는 검토된 **CN 한 명**에만 binding을 생성한다. 인증서에 관리자 그룹을 넣지 않고 RBAC에서 권한을 부여한다. [system:masters를 피하는 이유](https://v1-35.docs.kubernetes.io/docs/concepts/security/rbac-good-practices/)

```bash
kadmin create clusterrolebinding "$RKE2_BINDING" \
  --clusterrole=cluster-admin --user="$RKE2_USER_CN"
kadmin get clusterrolebinding "$RKE2_BINDING" -o yaml > "$RKE2_REVIEW_DIR/binding.yaml"
```

본인 장치에서 실제 사용자 확인과 작은 쓰기·읽기를 수행한다. 넉넉한 개발 관리자 권한에서는 다른 namespace 조회를 무조건 거부해야 한다는 제한 역할의 기준을 적용하지 않는다. 정책 질의만으로 끝내지 않는다.

```bash
kself auth whoami -o json
kself -n "$RKE2_TEST_NS" get configmap access-canary -o json
kself -n "$RKE2_TEST_NS" create configmap personal-write --from-literal=result=personal-client
kself -n "$RKE2_TEST_NS" get configmap personal-write -o json
kself -n "$RKE2_TEST_NS" delete configmap personal-write
```

인수 시 사전에 정한 짧은 회수 시험에서는 운영자가 기록한 binding의 이름·UID·subject·roleRef가 여전히 같은지 재확인하고 그 binding만 삭제한다. 다른 binding이나 서비스계정은 변경하지 않는다. 본인은 **같은 인증서·같은 정상 터널**로 새 요청을 보내며 실제 `Forbidden`을 확인한다.

```bash
# 운영자: 위에서 확인한 이 발급의 binding만 삭제한다.
kadmin delete clusterrolebinding "$RKE2_BINDING"
```

```bash
# 본인: 종료 코드만으로 성공 처리하지 않고 로그의 API 응답을 확인한다.
if kself -n "$RKE2_TEST_NS" get configmap access-canary \
  > "$RKE2_PERSONAL_DIR/revoked.log" 2>&1; then
  echo 'FAIL: 회수 뒤에도 리소스 조회가 허용됨' >&2
  exit 1
fi
```

거부되지 않으면 다른 User/Group binding·다른 자격 경로·객체 교체를 조사하고 완료로 기록하지 않는다. 인수에 포함해 승인한 **일시 회수·복구 시험**이라면 운영자가 동일 CN의 위 binding 생성 명령을 다시 실행하고 본인의 실제 조회 성공까지 확인한다. 이는 유효 인증서의 권한을 의도적으로 다시 연 것이다. 실제 퇴장·키 분실·침해 대응으로 회수한 CN은 다시 열지 않는다. 계속 접근이 필요하면 본인 장치의 새 키·새 CN으로 처음부터 재발급한다.

시험 namespace 정리는 기록한 시험 객체와 현재 UID·내용을 대조한 운영자가 맡는다. 업무 객체가 추가되었거나 소유가 다르면 삭제하지 않는다. 이 문서의 개인 키나 kubeconfig를 정리한다는 이유로 다른 세션·사용자 파일을 지우지 않는다.

## 6. 만료·회수와 인수 영수증

Kubernetes 1.35는 발급된 X.509 인증서의 개별 폐기를 지원하지 않는다. **CSR 삭제는 인증서 폐기가 아니다.** binding 제거는 권한 회수이며, 인증서는 만료 전까지 인증에 사용될 수 있고 기본 discovery 권한은 남을 수 있다. `system:masters` 그룹은 RBAC 제거로도 회수할 수 없어 발급하지 않는다. [Kubernetes 1.35 인증서 한계](https://v1-35.docs.kubernetes.io/docs/reference/access-authn-authz/authentication/#x509-client-certificates)

실제 만료 거부는 서명된 인증서의 `notAfter` 이후, 정상 터널에서 새 클라이언트 연결로 같은 업무 리소스를 요청해 확인한다. 기다리기 어려우면 별도 고유 CN·본인 키로 600초 시험 발급을 사용한다. 시스템 시계를 바꾸거나 로컬 날짜 출력만으로 만료 시험을 대신하지 않는다. 이전 장기 연결이 종료됐는지도 별도로 확인한다. 만료를 아직 관측하지 않았다면 `NOT_VERIFIED`로 남기며 8시간 요청·인증서 날짜 확인과 구분한다.

| 대상 | 종료·회수 시 확인 |
|---|---|
| 개인 Kubernetes 권한 | 해당 CN의 모든 승인 binding 검토·회수, 새 실제 요청의 거부. 이미 열린 exec/watch/port-forward 등의 연결도 별도 종료. |
| 개인 인증서 | 실제 만료, 키 분실 시 해당 CN 재사용 금지. 재발급은 새 키·고유 CN. 자동 갱신·자동 회수 서비스는 이 절차에 없음. |
| SSM 터널 | 본인 session ID를 명시적으로 종료하고 데이터 연결 종료를 확인. 창만 닫거나 SSO 로그아웃한 것을 종료 증거로 쓰지 않음. |
| SSO·AWS 역할 | 사용자 할당·로그인·발급된 IAM 역할 세션의 수명이 별개임을 확인. 신규 발급 차단만으로 기존 세션의 즉시 무효화를 주장하지 않음. |

종료 명령은 본인 또는 지정 회수 담당자의 승인된 터미널에서 실행한다. 새 터미널이면 자신의 프로필·기대 계정·리전을 설정하고 2절의 첫 환경 초기화·SSO 확인 블록을 먼저 실행한다. 터널을 열었던 B의 변수가 다른 종료 터미널에 전달된다고 가정하지 않는다.

```bash
: "${RKE2_PROFILE:?종료 담당자의 SSO 확인한 프로필}" "${RKE2_REGION:?해당 SSM 세션의 리전}"
: "${RKE2_SSM_SESSION_ID:?이 인수에서 기록한 본인 SSM session ID}"
aws ssm terminate-session --profile "$RKE2_PROFILE" --region "$RKE2_REGION" \
  --session-id "$RKE2_SSM_SESSION_ID"
```

SSM 세션 최대 수명은 별도 설정이며 인증서나 permission-set의 8시간과 자동 동기화되지 않는다. 현재 최대 수명 설정을 확인하고 필요 시 그 설정의 소유자와 조율한다. 이 문서 실행만으로 전역 Session Manager 설정을 바꾸지 않는다. [SSM 종료](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-end.html), [최대 세션 시간](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-preferences-max-timeout.html), [독립적인 AWS 역할 세션](https://docs.aws.amazon.com/singlesignon/latest/userguide/authconcept.html)

AWS `AdministratorAccess`와 Kubernetes `cluster-admin`을 가진 사람은 다른 접근 수단을 만들 수 있다. 따라서 이 개발 인수 절차는 악성 관리자에 대한 완전한 즉시 차단을 보장하지 않는다. 실제 퇴장·침해 대응에서는 추가 발급 자격·binding·열린 연결과 AWS 권한까지 담당자가 함께 검토한다. 포트 전달의 Kubernetes 요청 내용은 SSM 세션 로그에 남지 않으므로, AWS 세션 기록과 RKE2 감사 로그의 개인 CN·작업을 따로 확인한다. [SSM 포트 전달 로깅 한계](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html)

비공개 영수증은 본인 SSO 확인 시각/매핑, 클러스터·CA 지문, CSR UID·공개키 지문·CN, 승인자, 실제 만료, binding UID와 범위, 본인 `whoami`, 무자격/허용/일시 회수/복구/만료 결과, SSM session ID와 종료, 시험 객체 정리 및 남은 담당을 연결한다. 키·SSO 토큰·암호·관리자 kubeconfig는 영수증에 넣지 않는다. 공개본은 [공개 증빙 절차](public-evidence-policy.md)로 가리고 `PASS`/`FAIL`/`UNKNOWN`/`NOT_VERIFIED`를 보존한다.

파일 전달, CSR 승인, 터널 연결, 정책 질의 중 어느 하나만으로 개인 인수를 완료 처리하지 않는다. 정상 본인 접속과 실제 작업·대조군의 관측 범위만 보고한다. T1 전체 완료, 제품 콘솔 로그인, DB/PgBouncer·Executor 소비자 인수는 각 기존 계약의 별도 완료 기준이다.
