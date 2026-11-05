# AWS 플랫폼에서 목적지 API에 접속하기

T15 실행 도구와 T31 배포기는 서로 다른 egress·ServiceAccount를 쓴다. 이 디렉터리는 기존 operator와 별도 워크로드 tailnet을 이용하는 설치 입력을 렌더링한다. 클러스터를 변경하거나 인증 정보를 발급하는 명령은 없다. T15 planner·T10 승인·T31 제품 로직도 변경하지 않는다.

사람의 개발 장치 접속과 사전 생성 namespace의 시험 권한은 [개인 접속 프로필](PERSONAL.md)을 사용한다. 인증 프록시·개인 인증서·ExecCredential 설정을 환경별 입력으로 구분한다.

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
# T10 등록용 Collector API 경로

`collector-platform`은 기존 `platform-state` 안의 등록 Pod에 별도 API 경로를 추가한다.
T15/T31 경로와 자격은 재사용하지 않는다. 목적지의 기존 읽기 전용 Collector SA를
사용하며, SA·토큰·등록 Pod 생성은 등록 담당이 맡는다. 이 렌더러는 이 자원을 만들지 않는다.

- 호출 Pod label은 `app.kubernetes.io/name=t10-registration`과
  `bravo.dev/target-api-client=collector` 둘 다 필요하다. 네트워크 label은 SA 인증을 대신하지 않는다.
- 연결 주소는 `https://collector-target-api.platform-state.svc.cluster.local`이다.
  TLS server name은 운영자가 확인한 목적지 API의 canonical MagicDNS 이름이다.
  공인 CA 검증을 유지하고 기존 `tokenFile`로 목적지 Collector 토큰을 전달한다.
- 별도 `bravo-collector-egress`와 `collector-access` ProxyGroupPolicy를 사용한다.
  `collector-tailnet-fragment.json`을 **Bravo Clusters**의 현재 정책에 병합한다.
  전체 정책을 fragment로 대체하지 않는다. Collector는 목적지 API TCP 443만 허용한다.
- 공통 Tailnet·ProxyClass·operator와 기존 Namespace·DB 정책은 수정하지 않는다.
  기존 DB 연결은 등록 담당의 `bravo.dev/db-client=runtime` label과 별도 DB 정책이 담당한다.
  정책은 합산되므로 설치 전에 같은 Pod와 프록시를 선택하는 기존 정책을 확인한다.

```sh
python3 infra/envs/rehearsal-access/access.py collector-platform \
  --api-hostname "$TARGET_API_HOSTNAME" > collector-bootstrap.json
# 운영자는 namespace/cluster UID와 기존 정책을 확인하고 bootstrap을 순서대로 설치한다.
# 처음에는 DNS와 operator health만 허용한다. Service의 실제 포트를 읽은 뒤 아래를 실행한다.
kubectl -n tailscale-system get services -o json > observed-services.json
python3 infra/envs/rehearsal-access/access.py collector-network \
  < observed-services.json > collector-network.json
python3 infra/envs/rehearsal-access/access.py kubeconfig --role collector \
  --api-hostname "$TARGET_API_HOSTNAME" --token-file /credentials/token > collector-kubeconfig.json
```

`collector-network`는 Service UID·resourceVersion·전체 역할 label·443/TCP·실제 mapped
targetPort를 검증한다. 적용 직전에 같은 UID/resourceVersion인지 다시 대조한다.
프록시 Pod는 운영자가 새 정책을 적용하기 전까지 호출 트래픽을 받지 않는다.
실측은 같은 등록 label의 정상 경로 TLS 확인과 label 누락/다른 역할 경로 거부를
각각 수행한다. HTTP 401/403은 네트워크 거부가 아니다. Collector 신원의 허용/거부와
실제 등록 API 성공은 토큰을 인수한 등록 담당의 별도 결과로 기록한다.

이 경로는 Kubernetes API 조회용이다. `infra/envs/collector-network`의
OTel Collector → 메트릭 수신 경로와는 별개다.

## T15 자격 이전 시도의 namespace 권한 조립

`scoped_lifecycle.py`의 `ScopedPlanner`는 기존 `CredentialPlanner`를 감싼다.
호스트는 현재 T10 Admission과 실행 권한을 검증한 뒤 이 planner를 호출한다.
같은 소스의 파일을 호스트 이미지에 포함해 일반 Python 모듈로 import한다.
이 모듈은 HTTP 엔드포인트나 인증 기본값을 설치하지 않는다.

```python
planner = ScopedPlanner(credential_planner, provisioner=management_kubectl,
                        cleaner=management_uid_cleaner)
```

`credential_planner`의 Job 클라이언트는 `t15-runner`의 단기 tokenFile을 읽는다.
별도 관리 클라이언트는 기존 `NamespaceLifecycle`와 `T3Gate`에만 연결된다.
T3가 만드는 비교용 namespace도 이 관리 경로를 사용한다. 관리 자격을 제안자,
검증 Job, 후보 앱이나 AI 실행 계정에 전달하지 않는다.

기존 lifecycle이 새 namespace와 정본 T3 템플릿을 만든 뒤 UID·run·attempt를
확인하면, 관리 경로가 그 namespace에만 실행 Role/Binding을 만든다. 실행자는
자격 이전 검증에 필요한 Pod·Job·Service·ConfigMap·Secret·ServiceAccount와
로그를 사용한다. `rehearsal-intruder` 가장과 해당 namespace 한 개의 조회만
추가되며, namespace 생성·삭제, RBAC 변경, `bind`·`escalate`, Pod exec는 없다.
관리 namespace의 writer Job 권한은 기존 별도 Role을 사용한다.

권한 원문을 재조회하고 namespace UID를 다시 대조한 뒤 기존 verifier를
실행한다. 생성 실패·응답 불명·권한 또는 UID 변경이면 같은 요청을 재전송하지
않고 원 UID cleaner로 정리하며 실행을 열지 않는다. 기존 lifecycle wrapper는
조용히 버리지 않고 거부한다. 소유 namespace 삭제 후 ClusterRole/Binding도
ownerReference로 수거되므로 운영 정리에서는 이 객체들의 부재까지 확인한다.
GC를 즉시 권한 철회 보장으로 취급하지 않는다.

이 조립의 로컬 시험은 실제 T15 lifecycle과 모의 API를 사용한다. 실제 namespace
권한 검사, AWS 소비 Pod의 토큰 갱신·폐기, OpenBao writer, 후보 이미지와 실제
T10 계획의 해피패스는 각각 별도 운영 근거가 필요하다.
