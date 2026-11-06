# Product Reader 설정 v2 전환

#503 이후 Reader는 설정 v2(`job_identity`, 플랫폼 Job 토큰)만 받고 v1(`github_public_jwks_file`,
`sts_provider`)은 `invalid_product_configuration`으로 거부한다. 이전 Reader는 반대로 v2를 거부한다.
그래서 **새 Reader 이미지 고정값(#540 이후)과 설정 v2는 같은 rollout에서 바뀌어야** 한다.
이미지만 먼저 바뀌면 Reader가 시작하지 못한다(영향은 최초 제안 경로 읽기).

1. 운영 v1 설정·클러스터 `/openid/v1/jwks`·issuer로 v2 ConfigMap을 만든다(내용 해시 이름, 아직 미사용).
   `reader_config_v2.py --v1 … --jwks … --issuer …`. 비밀 값은 없다.
2. 새 이미지가 동기화될 때 Argo 런타임 값 `productReader.runtime.configuration`의
   `existingConfigMap`과 `sha256`을 v2 ConfigMap으로 바꾼다.
3. Reader Ready, 이미지 digest, 설정 sha256을 같은 기록에 남긴다. 실패하면 두 값을 함께 되돌린다.

2026-11-06 23:55 KST: v2 ConfigMap `t10-product-reader-config-v2-f08be14548cde543`을 만들었다
(issuer `https://kubernetes.default.svc.cluster.local`, JWKS 키 1개). 운영 Reader는 여전히 v1이다.
