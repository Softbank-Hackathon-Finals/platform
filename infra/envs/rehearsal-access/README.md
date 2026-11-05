# AWS 플랫폼에서 목적지 API에 접속하기

T15 실행 도구와 T31 배포기는 서로 다른 egress·ServiceAccount를 쓴다. 이 디렉터리는 기존 operator와 별도 워크로드 tailnet을 이용하는 설치 입력을 렌더링한다. 클러스터를 변경하거나 인증 정보를 발급하는 명령은 없다. T15 planner·T10 승인·T31 제품 로직도 변경하지 않는다.

## 설치 전 확인

- 양쪽 클러스터 UID, 변경 소유자, 실행 창, 기존 자원 UID를 인수한다. 같은 이름이 있으면 먼저 비교하고 새 자원을 중복 생성하지 않는다. 응답 유실 뒤 CREATE를 반복하지 않는다.
- AWS operator가 `Tailnet`, `ProxyGroup`, `ProxyGroupPolicy`, `ProxyClass` CRD를 지원해야 한다. [multi-tailnet](https://tailscale.com/docs/kubernetes-operator/manage-and-configure/multi-tailnet)은 operator 1.96 이상이 필요하다. 실제 설치된 CRD의 API 버전을 확인한다.
- 목적지 API는 [noauth API proxy](https://tailscale.com/docs/kubernetes-operator/api-server-access/noauth-mode)로 노출한다. 이 모드는 ServiceAccount bearer를 보존하며 Kubernetes가 인증·권한을 판단한다. 사람의 개인 접속과 별도다.
- 워크로드 tailnet의 정책은 `tag:bravo-rehearsal`, `tag:bravo-apply`에서 목적지 API 태그의 HTTPS만 허용한다. HTTP 리다이렉트를 사용하는 경우에만 80을 추가한다. DB·SSH와 무관한 기기는 거부한다. 제공자 정책 시험과 실제 패킷 시험을 각각 기록한다.
- operator의 OAuth Secret을 사전에 안전하게 공급한다. 이 tailnet의 Devices Core/Auth Keys/Services와 지정 operator 태그만 갖는 자격을 쓴다. 관리 OAuth를 워크로드에 넣지 않는다. 값은 명령행 인자·저장 파일·로그에 남기지 않는다.

## AWS 구성

아래 주소와 Secret 이름은 예시다. 실제 주소는 등록된 목적지와 대조한 비공개 설정에서 넣는다.

```sh
python3 infra/envs/rehearsal-access/access.py platform \
  --api-hostname target-api.example.ts.net \
  --credential-secret scoped-platform-operator > platform-access.json
```

출력은 별도 Tailnet, 역할별 egress ProxyGroup, namespace, SA, ExternalName Service, ProxyGroupPolicy와 NetworkPolicy다. 기존 cutover·DB 프록시를 교체하지 않는다. egress는 digest에 고정한 `tailscale/tailscale` 이미지가 필요하다. `tailscale/k8s-proxy`는 Kubernetes API 프록시용이라 egress에 넣으면 operator가 거부한다.

초기 출력은 DNS·operator health check만 허용한다. operator가 만든 Service의 실제 `targetPort`를 읽은 뒤 클라이언트 통신을 연다. 이 출력은 이름이 같은 기존 정책과 병합해 넓히는 용도가 아니라, 이 디렉터리가 소유하는 전용 정책을 교체하는 입력이다. 설치자는 UID와 현재 사양을 대조하고 적용·재조회를 수행한다.

```sh
kubectl --context PLATFORM get services -n tailscale-system \
  -l tailscale.com/parent-resource=target-api -o json > observed-services.json
python3 infra/envs/rehearsal-access/access.py network \
  < observed-services.json > platform-network.json
```

생성기는 원 namespace·ProxyGroup·관리 라벨·UID/resourceVersion·443/TCP·할당 포트를 검사한다. 한 역할의 Service가 없거나 중복되면 출력하지 않는다. 10000–10999 범위 전체를 열지 않고 현재 할당된 포트만 연다. Service가 다시 만들어져 포트가 바뀌면 새 UID/포트를 읽어 정책과 실제 통신을 다시 검증한다.

호출 Pod는 해당 control namespace에서 `bravo.dev/target-api-client: rehearsal` 또는 `apply` 라벨을 갖는다. 반대 역할의 proxy로 갈 수 없다. caller SA의 Kubernetes 토큰 자동 mount는 꺼져 있으며, 목적지 전용 단기 토큰은 별도 보호된 volume으로 공급한다.

```sh
python3 infra/envs/rehearsal-access/access.py kubeconfig \
  --role apply --api-hostname target-api.example.ts.net \
  --token-file /credentials/token > target.kubeconfig.json
```

이 kubeconfig에는 토큰 값이 없다. Service DNS로 연결하면서 목적지의 MagicDNS 이름을 TLS server name으로 사용한다. 검증을 끄거나 호스트의 current-context를 바꾸지 않는다. 운영 토큰 갱신·폐기와 namespace 수명주기는 별도 관리 주체가 맡는다.

## 목적지 권한

```sh
python3 infra/envs/rehearsal-access/access.py target-apply \
  --namespace target-app --deployment demo-chat > target-apply.json
```

T31은 지정한 Deployment 한 개의 get/watch/patch, 같은 namespace의 Pod get/list만 갖는다. Deployment 생성·삭제, pods/exec, Secret, RBAC 변경 권한은 없다. 초기 앱 배포는 T30의 승인된 배포 경로가 담당하며 T31은 검증·승인한 이미지 digest의 승격을 맡는다. 이 출력만으로 실제 앱이 배포되지는 않는다.

T15 AI 실행 신원은 기존 [T3 템플릿](../../../services/rehearsal-runner/templates/)의 `rehearsal-exec`를 사용한다. namespace를 만들고 지우는 관리 신원, 검증 Job을 구성하는 러너, AI 도구 신원을 구별한다. RBAC는 `rehearsal-*` 접두사만으로 namespace 생성 권한을 제한하지 못한다. 신뢰된 관리 경계와 UID 조건 삭제가 필요하다. 관리 러너가 읽을 시험 버킷 Secret 한 건을 AI 신원의 Role에 추가하면 안 된다. 같은 namespace의 Pod 생성 권한은 간접 Secret mount 가능성도 있으므로 보호된 검증 자격을 그곳에 두는 것으로 격리했다고 주장하지 않는다.

## 확인 및 정리

1. ProxyGroup과 Service의 Ready를 조회하고 실제 AWS Pod에서 TLS 검증을 켠 채 목적지 API에 접속한다. 무자격 401은 전송 경로 확인이며 인증 성공이 아니다.
2. 실제 단기 서비스계정으로 SelfSubjectReview 신원을 확인한다. 허용된 실제 create/get/delete 또는 독립 replicas=0 Deployment patch/get을 수행한다. 운영 앱을 시험용으로 수정하지 않는다.
3. 잘못된 토큰 401, Secret·다른 namespace·다른 Deployment·impersonation 403을 확인한다. SelfSubjectAccessReview의 `allowed=false`와 HTTP 오류를 혼동하지 않는다. PodSecurity 거부는 원 응답 사유까지 확인한다.
4. 허용 경로 성공 → 반대 역할 경로 차단 → 허용 경로 성공으로 네트워크를 대조한다. DNS 오류·TLS 오류·HTTP 401/403/503을 NetworkPolicy 차단 성공으로 분류하지 않는다.
5. 만든 Job/Pod/Secret/시험 namespace의 UID를 기록하고 동일 UID만 삭제한다. 부재를 다시 읽는다. 인계용 namespace를 유지하면 이름·UID·소유자·정리 책임을 명시한다.

[11월 5일 실측](../../../docs/evidence/t7-target-api-access-2026-11-05/README.md)은 이 구조의 실제 AWS→등록 목적지 검증이다. 이 생성기의 포트 제한 출력 적용과 T3 전체 격리·T15 실제 모델 리허설·T31 앱 rollout·T25 컷오버는 별도로 판정한다. 성공한 전송만으로 그 단계까지 완료 처리하지 않는다.
