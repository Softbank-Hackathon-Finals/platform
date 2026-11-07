# 온프렘 보안 탐지 설치 묶음 (T33)

목적지 온프렘 RKE2에 정책 탐지(Kyverno)와 런타임 탐지(Falco)를 두고, 결과를 플랫폼 수집 API로 **바깥 방향으로만** 보낸다([설계](../../../docs/security/detection-integration.md) 3절, 설계 문서 7.5). 플랫폼은 온프렘으로 들어가는 포트를 열지 않는다.

| 구성 | 고정 | 하는 일 |
|---|---|---|
| Kyverno | chart `kyverno/kyverno` 3.9.2(app v1.19.2), sha256 `f6fe7184…861e` | 정책을 **Audit**로만 검사해 PolicyReport(`wgpolicyk8s.io/v1alpha2`)를 남긴다. 웹훅은 실패해도 요청을 통과시킨다(`forceFailurePolicyIgnore`). 시스템 · 플랫폼 namespace는 웹훅에서 뺀다 |
| 정책 | `policies/require-run-as-nonroot.json` | 탐지 매핑의 `require-run-as-nonroot` → 범주 `POLICY`. 거부하지 않고 기존 Pod도 백그라운드로 검사한다 |
| Falco | chart `falcosecurity/falco` 9.2.2(app 0.45.2), sha256 `b8a1aa98…687d` | modern eBPF(커널 6.8, arm64, 커널 모듈 없음). falcosidekick이 경보를 `POST /v1/security/webhooks/falco/env-onprem`으로 보낸다 |
| 수집 에이전트 | `agent.json`(CronJob 5분) | PolicyReport를 읽고(get · list만) Falco DaemonSet 준비 상태를 확인해 `POST /v1/security/ingest/{kyverno\|falco}`로 보낸다. 읽지 못하거나 Falco가 준비되지 않으면 `agent_error`로 보내 화면이 "확인 불가"가 된다 |

토큰 · 주소는 Git에 없다. 설치할 때 stdin으로 Secret과 ConfigMap을 만든다.

## 아직 정해지지 않은 것 (설치 전에 필요)

- **플랫폼 수집 API 주소와 수집 자격:** 플랫폼 배포 PR에서 정한다. 자격은 `env-onprem` 범위, `ingest`, 출처 `kyverno` · `falco`, 24시간 이하이다(`docs/security/detection-hardening.md`).
- **목적지 → 플랫폼 경로:** T44 tailnet-egress(목적지) · tailnet-ingress(플랫폼)로 선언한다. 에이전트 · falcosidekick의 egress NetworkPolicy도 그때 같이 둔다.
- **에이전트 이미지:** `agent.json`의 digest는 자리표시다. 플랫폼 이미지 게시 뒤 그 digest로 바꾼다. 목적지의 비공개 이미지 pull 경로를 쓴다.

## 설치 순서 (해피패스가 끝난 뒤)

노드 하나(CPU 4 · 메모리 8GB)에 이미 업무 · DB · 프록시가 올라가 있다. 그래서 자원 상한을 낮게 두고, 하나씩 설치하며 노드 여유를 확인한다.

0. 클러스터를 세우는 쪽이 namespace `kyverno` · `falco` · `bravo-security`를 만든다(이 묶음은 namespace를 만들지 않는다).
1. Kyverno:
   ```sh
   helm upgrade --install kyverno kyverno/kyverno --version 3.9.2 -n kyverno -f kyverno-values.yaml
   kubectl get validatingwebhookconfigurations -l app.kubernetes.io/instance=kyverno \
     -o jsonpath='{range .items[*].webhooks[*]}{.name}{" "}{.failurePolicy}{"\n"}{end}'   # 모두 Ignore
   kubectl apply -f policies/
   kubectl get policyreports -A | head     # 백그라운드 검사 결과
   ```
2. Falco(주소 · 토큰은 stdin):
   ```sh
   printf 'WEBHOOK_ADDRESS=%s\nWEBHOOK_CUSTOMHEADERS=Authorization:Bearer %s\nWEBHOOK_CHECKCERT=true\nWEBHOOK_MINIMUMPRIORITY=notice\n' \
     "$FINDINGS_URL/v1/security/webhooks/falco/env-onprem" "$(cat "$INGEST_TOKEN_FILE")" \
     | kubectl -n falco create secret generic falcosidekick-bravo --from-env-file=/dev/stdin
   helm upgrade --install falco falcosecurity/falco --version 9.2.2 -n falco -f falco-values.yaml
   ```
3. 에이전트:
   ```sh
   kubectl -n bravo-security create configmap bravo-findings-agent --from-literal=url="$FINDINGS_URL"
   kubectl -n bravo-security create secret generic bravo-findings-agent-token --from-file=token=/dev/stdin < "$INGEST_TOKEN_FILE"
   kubectl apply -f agent.json      # digest를 바꾼 사본
   ```

## 확인 (T33 완료 기준의 온프렘 절반)

| 확인 | 기대 |
|---|---|
| 루트로 도는 Pod가 있음 | PolicyReport `fail` → 요약 `env-onprem` `POLICY` 칸에 건수 |
| 앱 Pod에 `kubectl exec`로 셸 | Falco 기본 규칙(컨테이너 안 터미널 셸) → `THREAT` 칸에 건수 |
| Falco DaemonSet을 0개로(없는 nodeSelector로 patch, DaemonSet은 scale이 없다) | 에이전트가 `SOURCE_UNHEALTHY`를 보내 `THREAT` 칸이 "확인 불가" |
| 에이전트 CronJob 일시정지 | 15분 뒤 칸이 "오래됨" |

운영자 Mac에서 먼저 확인하려면 에이전트를 밖에서 돌린다(같은 판정 경로):
`python -m bravo_findings.agent_run --kubectl-context <목적지 context>`

## 되돌리기

```sh
kubectl delete -f agent.json; helm uninstall falco -n falco; kubectl delete -f policies/; helm uninstall kyverno -n kyverno
kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations | grep -i kyverno   # 남은 웹훅이 없어야 한다
```

## 위험

- Kyverno 웹훅은 모든 요청을 거치지만 실패해도 통과(`Ignore`)다. Kyverno가 멈추면 탐지만 멈추고 배포는 막지 않는다.
- Falco는 권한 있는 DaemonSet이다(eBPF). 규칙은 falcoctl이 기본 색인에서 받는다. 목적지는 바깥 방향 인터넷이 열려 있다.
- falcosidekick은 경보 전문을 보낸다. 플랫폼 정규화기가 허용 필드만 남기고(`output` · 명령행 제외) 비밀 패턴을 가린다(detection-hardening 4절).
