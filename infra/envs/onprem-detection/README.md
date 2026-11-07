# 온프렘 보안 탐지 설치 묶음 (T33)

목적지 온프렘 RKE2에 정책 탐지(Kyverno)와 런타임 탐지(Falco)를 두고, 결과를 플랫폼 수집 API로 **바깥 방향으로만** 보낸다([설계](../../../docs/security/detection-integration.md) 3절, 설계 문서 7.5). 플랫폼은 온프렘으로 들어가는 포트를 열지 않는다.

| 구성 | 고정 | 하는 일 |
|---|---|---|
| Kyverno | chart `kyverno/kyverno` 3.9.2(app v1.19.2), sha256 `f6fe7184…861e` | 정책을 **Audit**로만 검사해 PolicyReport(`wgpolicyk8s.io/v1alpha2`)를 남긴다. 웹훅은 실패해도 요청을 통과시킨다(`forceFailurePolicyIgnore`). 시스템 · 플랫폼 namespace는 웹훅에서 뺀다 |
| 정책 | `policies/require-run-as-nonroot.json` | 탐지 매핑의 `require-run-as-nonroot` → 범주 `POLICY`. 규칙 단위 `failureAction: Audit`, `admission: false`라 업무 Pod 생성에 웹훅이 끼지 않고 백그라운드 검사만 한다. 시스템 · 플랫폼 namespace는 검사에서 빼고, 컨트롤러 규칙을 만들지 않아(autogen none) 같은 위반을 두 번 세지 않는다 |
| Falco | chart `falcosecurity/falco` 9.2.2(app 0.45.2), sha256 `b8a1aa98…687d` | modern eBPF(커널 6.8, arm64, 커널 모듈 없음). falcosidekick이 경보를 `POST /v1/security/webhooks/falco/env-onprem`으로 보낸다. 규칙은 설치 때 한 번 받고(follow 끔), Pod 로그에 경보를 남기지 않는다(stdout 끔) |
| 수집 에이전트 | `agent.json`(CronJob 5분) | PolicyReport를 읽고(get · list만) Falco DaemonSet · falcosidekick · Kyverno reports-controller 준비 상태를 확인해 `POST /v1/security/ingest/{kyverno\|falco}`로 500건씩 보낸다. 읽지 못하거나 하나라도 준비되지 않으면 `agent_error`로 보내 화면이 "확인 불가"가 된다. 3xx는 따라가지 않는다 |

토큰은 Git에 없다. 설치할 때 stdin으로 Secret을 만든다.

## 목적지 → 플랫폼 경로

| 무엇 | 값 |
|---|---|
| 주소 | `https://bravo-findings.bravo-security.svc:8443`. T44 `target-dev.json`의 egress 그룹 `bravo-target-findings`가 만드는 목적지 Service다. 플랫폼 Service와 이름 · namespace가 같아서 서버 인증서 SAN 하나로 맞는다 |
| tailnet | `tag:bravo-target-findings` → `tag:bravo-findings:8443`만(T44 정책 조각). 플랫폼 장치 `bravo-findings`는 `platform.json` tailnet-ingress |
| 송신 제한 | T44 클라이언트 정책(에이전트 · falcosidekick: DNS · proxy 포트만)에 에이전트 정책 `bravo-findings-agent`(DNS · API 서버 6443)를 더한다. 6443은 `kubernetes.default` 443이 Service DNAT 뒤 닿는 API 서버 끝점이다. 끝점 주소(노드 IP)는 설치마다 달라 Git에는 포트만 둔다. 설치 사본에서 `to: ipBlock <노드 IP>/32`로 좁힐 수 있다 |
| CA | 플랫폼 사설 CA 인증서(비밀 아님)를 ConfigMap `bravo-findings-ca`(키 `ca.crt`)로 `bravo-security` · `falco`에 둔다 |

## 아직 정해지지 않은 것 (설치 전에 필요)

- **수집 자격 두 개:** 모두 `env-onprem` 범위, `ingest`, 24시간 이하이다(`docs/security/detection-hardening.md`). falcosidekick은 출처 `falco`만, 에이전트는 `kyverno` · `falco`(Falco heartbeat)를 받는다. 플랫폼 레지스트리에는 sha256만 넣고 원문은 아래 Secret으로만 옮긴다. 보안 담당 운영자가 하루 한 번 만료 전에 다시 발급하고 Secret을 바꾼다(sidekick은 `rollout restart`, 에이전트는 다음 실행부터).
- **에이전트 이미지:** `agent.json`의 digest는 자리표시다. 플랫폼 이미지 게시 뒤 그 digest로 바꾼다. 목적지의 비공개 이미지 pull 경로를 쓴다.
- **차트 안 이미지 고정:** 차트는 sha256로 고정했지만 Kyverno · falco · falcoctl · falcosidekick · 플러그인 · 보조 이미지는 태그다. 설치 전에 각 digest를 받아 값 파일 사본에 `image.digest`(또는 `@sha256`)로 고정하고, Falco 규칙도 `falco-rules:5`가 아닌 정확한 판으로 고정한다.

## 설치 순서 (해피패스가 끝난 뒤)

노드 하나(CPU 4 · 메모리 8GB)에 이미 업무 · DB · 프록시가 올라가 있다. 그래서 자원 상한을 낮게 두고, 하나씩 설치하며 노드 여유를 확인한다.

0. 이 단계에서 운영자가 namespace를 만든다(T45 base가 아니다. 이 묶음은 namespace를 선언하지 않는다). T44 경로가 든 SHA를 목적지 Argo로 승격하기 전에 있어야 한다. 목적지 RKE2는 `profile: cis`라 기본 PSA가 restricted이므로 Falco namespace만 privileged로 둔다. CA ConfigMap도 둔다:
   ```sh
   for ns in kyverno:restricted falco:privileged bravo-security:restricted; do
     kubectl create namespace "${ns%%:*}" && kubectl label namespace "${ns%%:*}" pod-security.kubernetes.io/enforce="${ns##*:}"
   done
   for ns in bravo-security falco; do kubectl -n "$ns" create configmap bravo-findings-ca --from-file=ca.crt="$PLATFORM_CA_CERT"; done
   ```
1. Kyverno:
   ```sh
   helm pull kyverno/kyverno --version 3.9.2 -d "$WORK"
   echo "f6fe7184544d5e2ee33a77808c09e67aa603ce6560cc197038fde31d727c861e  $WORK/kyverno-3.9.2.tgz" | shasum -a 256 -c
   helm upgrade --install kyverno "$WORK/kyverno-3.9.2.tgz" -n kyverno -f kyverno-values.yaml --wait --timeout 5m
   # 실행 중에 만든 웹훅에는 webhook.kyverno.io/managed-by=kyverno만 붙는다. admission controller가 뜬 뒤에 생기므로 기다린다
   for _ in $(seq 30); do
     hooks=$(kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations -l webhook.kyverno.io/managed-by=kyverno \
       -o jsonpath='{range .items[*].webhooks[*]}{.name}{" "}{.failurePolicy}{"\n"}{end}')
     [ -n "$hooks" ] && break; sleep 5
   done
   if [ -z "$hooks" ]; then echo "멈춤: 150초 안에 Kyverno 웹훅이 생기지 않았다(기동 · 선택자 확인)" >&2
   elif printf '%s\n' "$hooks" | grep -v ' Ignore$' >&2; then echo "멈춤: 위 웹훅이 Ignore가 아니다" >&2
   else echo "웹훅 $(printf '%s\n' "$hooks" | wc -l | tr -d ' ')개 모두 Ignore"; fi
   kubectl apply -f policies/
   kubectl get policyreports -A | head     # 백그라운드 검사 결과
   ```
2. Falco(주소 · 토큰은 stdin):
   ```sh
   printf 'WEBHOOK_ADDRESS=%s\nWEBHOOK_CUSTOMHEADERS=Authorization:Bearer %s\nWEBHOOK_CHECKCERT=true\nWEBHOOK_MINIMUMPRIORITY=notice\n' \
     "https://bravo-findings.bravo-security.svc:8443/v1/security/webhooks/falco/env-onprem" "$(cat "$SIDEKICK_TOKEN_FILE")" \
     | kubectl -n falco create secret generic falcosidekick-bravo --from-env-file=/dev/stdin
   helm pull falcosecurity/falco --version 9.2.2 -d "$WORK"
   echo "b8a1aa984c3d513ab1639be760e6a07a9eb203d4da77a45a1512d382b6d3687d  $WORK/falco-9.2.2.tgz" | shasum -a 256 -c
   helm upgrade --install falco "$WORK/falco-9.2.2.tgz" -n falco -f falco-values.yaml
   ```
3. 에이전트:
   ```sh
   kubectl -n bravo-security create secret generic bravo-findings-agent-token --from-file=token=/dev/stdin < "$AGENT_TOKEN_FILE"
   kubectl apply -f agent.json      # digest를 바꾼 사본
   ```

## 확인 (T33 완료 기준의 온프렘 절반)

| 확인 | 기대 |
|---|---|
| 루트로 도는 Pod가 있음 | PolicyReport `fail` → 요약 `env-onprem` `POLICY` 칸에 건수 |
| 앱 Pod에 `kubectl exec`로 셸 | Falco 기본 규칙(컨테이너 안 터미널 셸) → `THREAT` 칸에 건수 |
| Falco DaemonSet을 0개로(없는 nodeSelector로 patch, DaemonSet은 scale이 없다) | 에이전트가 `SOURCE_UNHEALTHY`를 보내 `THREAT` 칸이 "확인 불가" |
| `falco-falcosidekick`을 0으로 scale | 같은 이유로 `THREAT` 칸이 "확인 불가"(경보 경로가 끊겼는데 "0건"으로 보이지 않는다) |
| `kyverno-reports-controller`를 0으로 scale | `POLICY` 칸이 "확인 불가" |
| 에이전트 CronJob 일시정지 | 15분 뒤 칸이 "오래됨" |

운영자 Mac에서 먼저 확인하려면 에이전트를 밖에서 돌린다(같은 판정 경로):
`python -m bravo_findings.agent_run --kubectl-context <목적지 context>`
Mac은 tailnet 경로(목적지 태그 전용)를 쓰지 않는다. 플랫폼 `svc/bravo-findings`를 port-forward하고 인증서 이름 `bravo-findings.bravo-security.svc`를 hosts에서 127.0.0.1로 둔 채 같은 주소와 `BRAVO_FINDINGS_CA_FILE`로 보낸다.

## 되돌리기

```sh
kubectl delete -f agent.json; helm uninstall falco -n falco; kubectl delete -f policies/; helm uninstall kyverno -n kyverno
kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations -l webhook.kyverno.io/managed-by=kyverno   # 남은 웹훅이 없어야 한다
```

## 위험

- 정책은 `admission: false`라 업무 Pod 생성이 Kyverno를 기다리지 않는다. Kyverno 자체 웹훅(정책 객체 검사 등)은 남지만 실패해도 통과(`Ignore`)다. Kyverno가 멈추면 탐지만 멈추고(`POLICY` "확인 불가") 배포는 막지 않는다.
- Falco는 권한 있는 DaemonSet이다(eBPF). 규칙은 설치 때 falcoctl이 기본 색인에서 한 번 받는다(follow 끔). 목적지는 바깥 방향 인터넷이 열려 있다. 이미지 · 규칙 판을 정확히 고정하기 전에는 설치하지 않는다(위 "차트 안 이미지 고정").
- falcosidekick은 경보 전문을 보낸다. 플랫폼 정규화기가 허용 필드만 남기고(`output` · 명령행 제외) 비밀 패턴을 가린다(detection-hardening 4절).
