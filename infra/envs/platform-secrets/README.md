# 플랫폼 Edge 제어키와 OpenBao CRD

Edge 제어키의 정본은 플랫폼 전용 OpenBao KV v2의 `edge-t17/cloudflare`다.
ESO의 `ExternalSecret`이 30초마다 `platform-state/t17-cloudflare-lb`에 반영하고,
Edge 컨트롤러는 매 요청마다 마운트 파일을 다시 읽는다. 키는 Git, Helm values,
Terraform state, 로그에 넣지 않는다. Cloudflare 토큰 발급과 폐기는 provider 작업이며
ESO 동기화가 새 provider 토큰을 발급하거나 이전 토큰을 폐기하지는 않는다.

서버의 실제 관리자는 [KubeBao OpenBao Operator 0.6.0](https://github.com/kubebao/openbao-operator/releases/tag/0.6.0)이다.
`OpenBaoCluster`가 서버·TLS·unseal·보존 저장소의 수명주기를 관리한다.
OpenBao 공식 조직의 보관된 Secrets Operator와 별개인 커뮤니티 프로젝트다.
API는 `v1alpha1`이며 업그레이드는 버전과 이미지 digest를 고정해 검증한다.
Operator는 `platform-secrets` 한 namespace만 관리한다. 기존 목적지의
`bravo-secrets/target-openbao`와 그 데이터·정책·키는 이 CR의 소유 범위에 없다.

이 구성은 단일 노드 해커톤 플랫폼용 `Development` profile과 보존 local PV를 사용한다.
unseal 키는 Operator가 immutable Secret으로 생성하고 재시작 때 재사용한다.
자체 초기화의 임시 root token은 폐기되며 사용자에게 수동 init/unseal을 요구하지 않는다.
외부 KMS·HA·외부 백업을 구성한 `Hardened` 배포는 아니다.
OpenBao 2.7.2를 고정했으며 upstream 0.6.0의 주 검증 버전 2.6.4/2.7.1과
다르므로 실제 RKE2 초기화·재시작·키 전달을 별도로 확인해야 한다.

## 적용 순서

1. 현재 플랫폼의 cluster/namespace/node UID를 대조한다. `prepare.py --output <새 경로>`는
   고정 소스 SHA·archive hash·CRD hash와 ESO 2.14.0 chart hash를 검사한다.
2. `render.py foundation --node <검증한 노드>`로 새 namespace와 Retain PV를 만든다.
   host의 `/var/lib/bravo-platform/openbao`는 기존 경로가 없음을 확인해 전용 소유로 생성한다.
   PVC는 Operator가 만든 `data-platform-openbao-0`만 결속한다. DB PV를 재사용하지 않는다.
3. 검증한 chart의 CRD를 먼저 적용하고 `operator-values.yaml`로 Operator를 설치한다.
   admission enforcement를 유지한다. 서버 StatefulSet을 별도 Helm이나 수동 patch로 관리하지 않는다.
4. `render.py cluster --api-endpoint <실제 API endpoint IP>`의 `OpenBaoCluster`를 적용한다.
   API·TLS·Initialized·Unsealed·PVC UID를 읽고 재시작 시 같은 데이터가 유지되는지 확인한다.
5. `eso-values.yaml`로 ESO를 `platform-external-secrets`에 설치한다. 주 controller는
   `platform-state`만 처리한다. CRD/webhook/cert-controller의 기반 권한은 별개다.
6. Operator가 발급한 실제 공개 CA를 `render.py delivery --public-ca <PEM>`에 입력한다.
   writer SA의 audience 제한 JWT로 지정 KV 한 경로에 CAS 쓰기하고 reader SA로 읽는다.
   writer는 키 값을 읽지 못하고 reader는 쓰기·역할/정책 관리 권한이 없다.
7. `SecretStore` Valid, `ExternalSecret` Ready, 실제 Cloudflare GET, 다른 SA/다른 경로 거부,
   실제 키 교체와 consumer 반영, 재시작 후 동일 UID·KV version을 확인한다.
   ESO Ready만으로 실제 Edge 제어 완료를 주장하지 않는다.

`selfInit.requests`는 최초 초기화에만 적용된다. 기존 서버의 사용자 정책·역할 변경을
CR 편집만으로 재적용한다고 가정하지 않는다. `reconcilePolicies`는 upstream의
승인된 운영 정책을 관리한다. 초기화·기존 KV 변경은 별도 소유와 CAS를 확인한다.

키 교체는 새 provider 토큰 검증 → OpenBao CAS 갱신 → 실제 consumer GET →
이전 provider 토큰 폐기 순서다. 응답 불명 시 새 토큰/새 KV version을 무조건 만들지 않고
같은 발급 ID·KV metadata를 조회한다. CR 삭제·Helm 제거를 데이터 삭제로 쓰지 않는다.
