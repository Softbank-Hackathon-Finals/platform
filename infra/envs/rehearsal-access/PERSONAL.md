# 개인 접속 프로필과 격리 시험 권한

`personal.py`는 검토한 환경별 JSON에서 kubeconfig와 RBAC를 생성한다. Python 표준 라이브러리만 필요하다. 고정 이메일, tailnet, 클러스터 주소, namespace 이름을 코드에 넣지 않는다. 기존 [워크로드 접속](README.md), T15 러너와 T3 템플릿은 그대로 사용한다.

## 입력과 인증 방식

[프로필 예시](personal.example.json)의 모든 식별자는 가상 값이다. 운영자는 실제 API 주소, `kube-system` UID, **이미 생성한 전용 namespace의 UID**와 선택한 시험 관리 namespace의 UID, 개인 신원, 필요한 리소스와 동작을 확인해 비공개 프로필을 만든다. `name`은 개인정보 없는 발급별 고유 이름이다. JSON의 알 수 없는 필드는 거부한다. 다른 namespace를 같은 프로필의 쓰기 대상으로 추가할 수 없다.

| auth.mode | 인증 입력 | 사용 환경 |
|---|---|---|
| `tailscale-proxy` | 자격 값 없음; 선택 `proxyServiceAccount` | 개인 Tailscale 신원을 전달하는 인증 프록시 |
| `client-certificate` | `certificateFile`, `keyFile`의 본인 장치 절대경로 | 직접 API 또는 TLS를 그대로 전달하는 터널 |
| `exec` | 승인한 실행 파일의 절대경로 `command`, 배열 `args`, `interactiveMode` | OIDC, 클라우드 CLI 등 Kubernetes ExecCredential v1 제공 도구 |

`cluster.trust`는 `{"mode":"system"}` 또는 `{"mode":"ca-file","caFile":"/absolute/server-ca.pem"}`이다. IPv4·IPv6·DNS, HTTPS 포트, 필요한 `tlsServerName`을 입력한다. Linux/WSL/macOS와 Windows 절대경로를 지원한다. 시스템 인증서를 쓰는 HTTPS 프록시에 Kubernetes 내부 CA를 대신 지정하지 않는다. `insecure-skip-tls-verify`, 인라인 토큰·개인 키, 임의 환경 변수는 받지 않는다. exec 설정은 수신 장치에서 프로그램을 실행하므로 전달 전 실행 파일과 인자를 검토한다. 생성기는 그 프로그램을 실행하지 않는다.

CSR 발급이 필요한 환경은 [인증서 절차](../../../docs/rke2-personal-access.md)를 따른다. 그 문서의 사람용 관리자 권한 예시를 이 시험 역할에 합치지 않는다. 개인 키는 본인 장치에서만 만들고 보관한다.

인증 프록시는 사용자에게 부여한 Role 외에, 그 사용자의 신원을 API에 전달하는 `impersonate users` 권한도 필요하다. `auth.proxyServiceAccount`에 실제 프록시의 `name`·`namespace`를 지정하면 별도 ClusterRole/Binding을 생성한다. 이 권한은 **해당 프록시 SA가 `username` 한 명을 대신하는 동작만** 허용하며 사람·시험 관리자에게 부여하지 않는다. 다른 사용자, 그룹, SA의 가장 권한은 추가하지 않는다. 기존 operator 역할을 수정하지 않으며 발급별 객체로 회수한다. 생략할 수 있는 경우는 같은 제한의 프록시 권한을 별도로 제공·확인한 환경이다. 인증서·exec 모드에서는 이 필드를 거부한다.

## 운영자 준비

```sh
umask 077
python3 infra/envs/rehearsal-access/personal.py rbac --profile private-profile.json > personal-rbac.json
python3 infra/envs/rehearsal-access/personal.py kubeconfig --profile private-profile.json > personal.kubeconfig.json
```

출력만으로 클러스터는 바뀌지 않는다. 다음 순서로 인수한다.

1. 사전 합의한 시험 namespace를 준비하고 T3의 `restricted` Pod Security, quota, NetworkPolicy를 확인한다. 기존 namespace를 재사용하면 소유자와 실제 UID를 대조한다. 운영 자격·Secret을 넣지 않는다. Pod 생성 권한은 그 namespace의 SA·Secret을 사용할 수 있으므로 Secret get 거부만으로 분리했다고 주장하지 않는다.
2. **명시한 운영자 kubeconfig**로 `kube-system`과 시험 namespace를 JSON으로 읽고 `verify`로 UID·종료 상태·PSA를 확인한다. 다른 context의 current-context에 의존하지 않는다.
3. 생성한 권한의 리소스·동작·개인 신원을 검토한다. 같은 이름의 Role/Binding/SA가 있으면 덮어쓰지 말고 UID와 소유를 비교한다. `kubectl create --dry-run=server -f personal-rbac.json`을 통과한 신규 객체만 `create`한다. 일부 성공 또는 응답 유실 뒤에는 재전송 전에 객체를 읽어 실제 결과를 확인한다.
4. 인증 프록시 환경이면 실제 프록시 Deployment의 ServiceAccount와 설정을 대조한다. 그 SA가 `username`의 `users`를 impersonate할 수 있는지 SubjectAccessReview로 확인하고, 다른 사용자·그룹·SA에 대한 신규 허용이 없는지 확인한다. 사용자 본인의 RBAC 검사만으로 이 전달 경로를 검증했다고 판단하지 않는다.
5. 실제 RBAC의 허용·거부와 개인 로그인은 따로 기록한다. 운영자의 `--as` 검증은 개인 장치 로그인 증거가 아니다. 재생성된 namespace에 이 프로필을 적용하지 않는다.

```sh
python3 infra/envs/rehearsal-access/personal.py verify --profile private-profile.json \
  --cluster-readback cluster.json --namespace-readback namespace.json \
  --runner-namespace-readback manager-namespace.json
```

`testRunner`를 사용하면 해당 관리 namespace도 읽어 `--runner-namespace-readback`으로 전달한다. 사용하지 않으면 이 옵션은 생략한다.

사람에게는 지정 namespace의 명시한 리소스만 쓸 수 있는 Role을 부여한다. 외부 namespace는 Pod·NetworkPolicy 읽기만 선택할 수 있다. 클러스터 범위 권한은 `kube-system`과 시험 namespace의 **get**뿐이다. 시험 관리 namespace를 지정하면 그 namespace의 get도 포함한다. namespace 생성·삭제·수정, node, clusterrole, wildcard, bind, escalate, deletecollection, 서비스계정 토큰 발급은 없다. Role/RoleBinding 작성은 Kubernetes의 권한 상승 방지 검사도 받는다. 리소스 목록의 상한은 코드의 보안 정책이며, 환경별 값과 구분한다. 새 API 지원은 검토와 경계 시험 후 그 상한에 추가한다.

## 본인 WSL 또는 개발 장치

초대를 받은 본인 계정으로 연결한다. 전달받은 kubeconfig는 별도 파일에 저장하며 기존 파일과 current-context를 덮어쓰지 않는다. 프로필을 본인 장치에서 생성할 수도 있다. 권한 파일은 운영자에게만 필요하다.

```sh
kubectl --kubeconfig=personal.kubeconfig.json --request-timeout=15s auth whoami -o json > whoami.json
kubectl --kubeconfig=personal.kubeconfig.json --request-timeout=15s get namespace kube-system -o json > cluster.json
# 아래 이름은 받은 프로필의 namespace.name을 사용한다.
kubectl --kubeconfig=personal.kubeconfig.json --request-timeout=15s get namespace sandbox-member -o json > namespace.json
# testRunner를 사용하면 관리 namespace도 본인 신원으로 읽는다.
kubectl --kubeconfig=personal.kubeconfig.json --request-timeout=15s get namespace access-control -o json > manager-namespace.json
python3 infra/envs/rehearsal-access/personal.py verify --profile private-profile.json \
  --cluster-readback cluster.json --namespace-readback namespace.json --whoami-readback whoami.json \
  --runner-namespace-readback manager-namespace.json
```

`personalLoginVerified=true`는 본인이 방금 조회한 원문을 넣었을 때의 신원·UID 대조 결과다. 도구 자체가 원격 조회나 개인 장치 소유를 증명하지 않는다. 초대 수락, TLS 접속, RBAC, 전체 T3 격리 통과를 각각 기록한다.

## 인증 프록시에서 서비스계정 격리 시험

Tailscale 인증 모드는 클라이언트의 Authorization·Impersonate 헤더를 제거하고 본인 신원으로 바꾼다. 따라서 WSL에서 보낸 `--as`나 bearer token으로 다른 계정을 시험할 수 없다. [공식 인증 계약](https://tailscale.com/docs/kubernetes-operator/api-server-access/auth-and-rbac), [구현](https://github.com/tailscale/tailscale/blob/v1.104.4/k8s-operator/api-proxy/proxy.go)

선택 입력 `testRunner`는 **AI가 Pod를 만들 수 없는 별도 관리 namespace**에 시험 관리자 SA를 만든다. 관리 namespace의 이름·UID, 고정 Pod 이름, 실제 API IP·포트를 함께 입력한다. 같은 namespace를 지정하면 거부한다. 이 계정은 같은 개인 Role을 사용하며, 지정한 **리허설 namespace의 SA 한 개만** 가장할 수 있다. AI 계정에는 관리 권한이 추가되지 않는다. 사람은 관리 namespace에서 지정한 Pod 한 개의 get·logs·exec만 받으며 Pod 생성·Secret·RBAC 수정 권한은 받지 않는다. 이 관리자 SA를 쓰는 단기 Pod에서 Kubernetes 내부 API로 T3 시험을 실행하면 `--as=system:serviceaccount:<시험 namespace>:<시험 SA>`를 사용할 수 있다. Role은 namespace가 포함된 ServiceAccount impersonation 요청만 허용한다. [Kubernetes 인증 필터](https://github.com/kubernetes/kubernetes/blob/v1.34.0/staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation.go)

```sh
python3 infra/envs/rehearsal-access/personal.py probe --profile private-profile.json \
  --service-account human-checker --pod-name personal-checker \
  --image 'registry.example.test/kubectl@sha256:<검토한 실제 digest>' > checker.json
```

`cluster.inClusterServer`에는 해당 클러스터 내부에서 TLS 검증 가능한 API 주소를 넣는다. Pod에는 projected SA 토큰·CA의 **파일 경로만** 참조하는 kubeconfig가 내려간다. 토큰 값은 manifest·명령행에 들어가지 않는다. kubectl이 기본 localhost:8080으로 접속하지 않도록 이 구성을 명시한다.

Pod 출력은 30분 제한, non-root, 읽기 전용 root filesystem, seccomp, capabilities 제거를 적용한다. 실제 이미지의 CPU 아키텍처, `sleep`·`kubectl` 포함 여부를 확인한다. 실행 전에 Pod의 SA·이미지를 검토하고 고유 Pod 이름으로 생성한다. 필요하면 승인한 시험 도구가 든 이미지로 바꾼다. 생성기는 이미지를 빌드하거나 내려받지 않는다.

`personal.py network --profile private-profile.json`은 관리 namespace의 default-deny와 시험 Pod만 선택하는 **실제 API Service/Endpoint IP·포트** egress를 생성한다. IPv4는 /32, IPv6는 /128로 제한한다. 이 관리 정책은 T3 시험 namespace의 정책과 별개다. 전체 namespace에 API 또는 인터넷을 열지 않는다. 이 Pod로 `kubectl auth whoami`와 `--as` 신원을 먼저 대조한 후 시험한다. 테스트 매니저에서 측정한 결과와 AI SA에서 측정한 결과를 분리한다. 전체 T3 스크립트·ADR 채택은 기존 담당자의 범위다.

## 회수와 정리

시험 시간대를 조율한 뒤 실제 자원의 UID를 기록한다. namespace의 생성·삭제는 운영자가 맡는다. Kubernetes RBAC는 이름 접두사로 namespace create를 제한하거나 UID 조건을 강제하지 못한다. 삭제 요청에는 `DeleteOptions.preconditions.uid`를 넣고 재생성·불일치 시 중단한다. Pod, 선택한 시험 관리자 SA, 프로필 전용 Role/Binding과 ClusterRole/Binding도 생성한 UID와 이름으로만 회수하고 부재를 확인한다. 인계 목적으로 namespace를 유지하면 소유자·정리 책임·재개 조건을 기록한다. 어떤 객체도 넓은 label selector로 일괄 삭제하지 않는다.
