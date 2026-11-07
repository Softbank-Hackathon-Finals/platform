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

위가 갖춰지고 이미지 digest가 고정된 뒤(`publish-security-findings-image` → 고정 PR), 개발 값에서 `securityFindings.enabled`를 켠다. 감사는 지금 SQLite `audit` 표에만 남는다. T24 감사 저장소 연동은 운영 인수 조건이므로(`docs/security/detection-hardening.md`) 켜기 전에 연동 계획을 맞춘다. 아직 하지 않은 것: AWS GuardDuty 주기 수집(플랫폼 OIDC → 읽기 전용 IAM 역할)과 온프렘 수집 경로(T44 tailnet-ingress · egress).
