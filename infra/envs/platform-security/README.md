# T33 보안 탐지 API의 플랫폼 전제

보안 탐지 수집 · 조회 API(`services/security-findings`)는 플랫폼 Argo가 GitOps로 배포한다(`securityFindings.enabled`). 그 전에 클러스터를 세우는 쪽이 아래를 한 번 만든다. 워크로드는 이 폴더에 없다.

| 무엇 | 어디서 | 비고 |
|---|---|---|
| namespace `bravo-security`(restricted) · 정적 PV · namespace 기본 거부 | `render.py --node <플랫폼 노드>` | PV는 `platform-local`(자동 생성 없음), 1GiB, `Retain`, claim `bravo-security/bravo-findings-data`에 묶임 |
| 노드 폴더 `/var/lib/bravo-platform-data/security-findings` | 플랫폼 데이터 디스크(`infra/envs/platform-data`)와 같은 운영자 경로(SSM) | `install -d -o 10001 -g 10001 -m 2770`. Pod의 `fsGroupChangePolicy: OnRootMismatch`가 이 폴더를 맞는 것으로 보고 마운트 때 소유권을 다시 바꾸지 않는다(바꾸면 SQLite DB가 0660이 되어 API가 열지 않는다). DB는 0600으로 만들어진다 |
| Secret `bravo-findings-tls` | 플랫폼 사설 CA로 발급 | SAN `bravo-findings.bravo-security.svc` 하나. 온프렘도 같은 이름으로 부른다(목적지 egress Service가 같은 이름 · namespace, T44 `target-dev.json`). 온프렘에는 이 CA 인증서만 배포한다 |
| Secret `bravo-findings-auth` | 자격 레지스트리 v1(`docs/security/detection-hardening.md` 2절), stdin으로 생성 | 토큰 원문이 아니라 sha256만. 24시간 이하. Pod가 init에서 0400 일반 파일로 옮기므로 교체 뒤에는 Pod를 다시 띄운다. **교체:** 보안 담당 운영자가 만료 전에(하루 한 번) 새 자격을 발급해 Secret을 바꾸고 `kubectl -n bravo-security rollout restart deploy/bravo-findings`. 자동 교체는 T10 신원 연동 뒤 |
| Secret `platform-ghcr-reader` | 다른 플랫폼 namespace와 같은 읽기 Secret | 비공개 GHCR 이미지 pull |
| 플랫폼 Argo 클러스터 자격 namespace 목록에 `bravo-security` 추가 | `infra/gitops/environments/development/public-entry.json`의 `clusterCredentials.platform.namespaces` | 켤 때 함께 바꾼다. 빠지면 차트가 렌더를 거부한다 |
| 조회 클라이언트 namespace 라벨 `bravo.security/findings-client=true` | 콘솔 · SOVA MCP가 있는 namespace | 라벨이 없으면 API NetworkPolicy가 받지 않는다 |

```sh
python3 infra/envs/platform-security/render.py --node "$PLATFORM_NODE" > "$PRIVATE/platform-security.json"
kubectl apply --dry-run=server -f "$PRIVATE/platform-security.json" && kubectl apply -f "$PRIVATE/platform-security.json"
```

위가 갖춰지고 이미지 digest가 고정된 뒤(`publish-security-findings-image` → 고정 PR), 개발 값에서 `securityFindings.enabled`를 켠다. 감사는 지금 SQLite `audit` 표에만 남는다. T24 감사 저장소 연동은 운영 인수 조건이므로(`docs/security/detection-hardening.md`) 켜기 전에 연동 계획을 맞춘다.

## AWS GuardDuty 주기 수집 (CronJob `bravo-findings-pull`, 5분)

플랫폼 웹 신원으로 읽기 전용 역할을 받는다. 장기 키를 만들지 않는다.

| 무엇 | 어디서 | 비고 |
|---|---|---|
| IAM 역할 `bravo-findings-pull` | 이 폴더의 terraform(`main.tf`) | 신뢰: 플랫폼 발급자의 `system:serviceaccount:bravo-security:bravo-findings-pull` · `aud=sts.amazonaws.com` 하나. 권한: `guardduty:ListDetectors`(자원 유형이 없어 `*`, 한 리전 조건) · `GetDetector` · `ListFindings` · `GetFindings`(이 계정 · 리전의 `detector/*`)만 |
| 플랫폼 발급자 OIDC provider | `infra/envs/platform-gate-signer`(먼저 적용) | 계정에 발급자 URL마다 하나뿐이라 이 스택은 읽기만 한다 |
| ConfigMap `bravo-findings-aws` | 운영자(Git 밖) | `account_id`, `role_arn`(terraform 출력). 비밀은 아니지만 계정 식별자라 Git에 두지 않는다 |

```sh
terraform -chdir=infra/envs/platform-security init -backend-config="$PRIVATE/platform-security.backend.hcl"
terraform -chdir=infra/envs/platform-security plan -var-file="$PRIVATE/platform-security.tfvars" -out="$PRIVATE/platform-security.plan"
terraform -chdir=infra/envs/platform-security apply "$PRIVATE/platform-security.plan"     # 역할 1 · 인라인 정책 1
kubectl -n bravo-security create configmap bravo-findings-aws \
  --from-literal=account_id="$ACCOUNT_ID" --from-literal=role_arn="$(terraform -chdir=infra/envs/platform-security output -raw role_arn)"
```

GuardDuty 제어 API에는 인터페이스 엔드포인트가 없어서 수집 Pod만 443 바깥 방향을 연다(GuardDuty 읽기 자격만 가진다). 클러스터 안(Pod 10.42/16 · Service 10.43/16)과 링크 로컬(IMDS)은 뺀다. 수집 Job과 API가 같은 SQLite에 쓴다(`BEGIN IMMEDIATE` · `busy_timeout` 5초). CronJob은 `startingDeadlineSeconds: 120`이라 컨트롤러가 오래 멈춰도 놓친 일정을 쌓지 않는다. 수집이 실패하면 출처가 `FAILED`가 되고 화면 칸은 "확인 불가"다.

온프렘 수집 경로는 T44 선언(`platform.json` tailnet-ingress `bravo-findings`, `target-dev.json` egress 그룹 `bravo-target-findings`)으로 따로 연다.

## Google Cloud SCC 주기 수집 (CronJob `bravo-findings-scc-pull`, 5분)

플랫폼 웹 신원으로 GCP workload identity pool을 거쳐 읽기 전용 reader 서비스 계정을 가장한다. 서비스 계정 키는 없다. GCP 쪽 설정은 [`infra/envs/gcp-scc-reader`](../gcp-scc-reader/README.md)(#940)가 만든다.

| 무엇 | 어디서 | 비고 |
|---|---|---|
| pool · provider · reader | `infra/envs/gcp-scc-reader` terraform | 주체 `system:serviceaccount:bravo-security:bravo-findings-scc-pull` 하나, 토큰 audience는 고정 이름 `bravo-findings-scc` 하나(그래서 이 매니페스트에 프로젝트 번호가 없다) |
| ConfigMap `bravo-findings-scc` | 운영자(Git 밖) | `scope.json`(terraform `scope` 출력), `credentials.json`(`credential_configuration` 출력, 토큰 파일 `/var/run/secrets/gcp/token`). 비밀은 아니지만 프로젝트 번호 · 서비스 계정 주소가 있어 Git에 두지 않는다 |
| SCC 활성 | GCP 콘솔(스택 밖) | 꺼져 있으면 출처가 `NOT_ENABLED`, 연합 · 권한이 틀리면 `ACCESS_DENIED`. 어느 쪽도 "0건"이 아니다 |

```sh
kubectl -n bravo-security create configmap bravo-findings-scc \
  --from-file=scope.json=<private>/scc-scope.json --from-file=credentials.json=<private>/scc-wif.json
```

API의 출처 목록(`BRAVO_FINDINGS_SOURCES`)에 `gcp_scc`/`env-gcp`가 있어서, 위가 갖춰지기 전에는 `env-gcp` 칸이 "확인 불가"로 보인다. 수집 Pod는 443 바깥 방향만 연다(googleapis, 클러스터 안과 링크 로컬 제외).
