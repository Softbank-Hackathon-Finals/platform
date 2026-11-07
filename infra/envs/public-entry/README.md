# 플랫폼 공개 입구 (GitOps)

공개 호스트를 이 폴더의 `entries.json`에만 선언한다. 병합하면 플랫폼 Argo(`platform-public-entry` Application)가
조정기를 배치하고, 조정기([services/public-entry](../../../services/public-entry/README.md))가 Cloudflare를 선언대로 맞춘다.
사람이 대시보드 · API · kubectl로 Access · Tunnel · DNS를 만들지 않는다.

| 무엇 | 어디서 |
|---|---|
| 호스트 → 클러스터 안 Service | `entries.json` (Git) |
| Access 허용 이메일 · Cloudflare 토큰 · 계정 · zone | 플랫폼 OpenBao `platform/edge/cloudflare-gitops`(`token`, `account_id`, `zone_id`, `access_emails`) → ESO |
| UI를 켜는 Argo 값(읽기 전용, 로그인 없음) | `infra/gitops/environments/development/public-entry.json` (bootstrap이 함께 넘긴다) |
| 플랫폼 노드의 Cloudflare 엣지 7844 바깥 방향 | `infra/envs/platform-aws` SG 규칙 `cloudflare_tunnel` |

- 항목에 `"public": true`를 두면 그 호스트는 Access 없이 열린다(로그인 · origin Access 검사 없음). 플랫폼 Argo(`platform-argocd`)는 2026-11-08 사용자 결정으로 공개다: 읽기 전용 · 로그 없는 viewer라 Application · 프로젝트 상태와 라이브 매니페스트(Secret 값 제외)가 인터넷에 보인다. 보호 항목이 하나도 없으면 조정기가 소유한 Access 앱 · 정책을 지운다.
- 사람 인증은 Cloudflare Access다. 모든 Tunnel 경로가 origin에서도 Access 토큰을 요구하므로, DNS를 우회한 요청도 Argo에 닿지 않는다.
  Argo 화면은 읽기 전용이고 바꾸는 계정이 없다. 변경은 계속 Git PR → 컨트롤러다.
- 플랫폼 Argo의 경계는 그대로다: `platform-state`만, 이름으로 묶은 네 객체(Deployment · ConfigMap · NetworkPolicy · ExternalSecret)만,
  Kubernetes API 자격 없는 Pod만(이름 정책 VAP). Tunnel 토큰은 조정기와 연결기가 나누는 메모리 볼륨 파일에만 있다.
- 고객 클러스터의 화면은 그 클러스터가 tailnet 장치로만 노출하고, 이 플랫폼 Tunnel이 중계한다(고객 쪽 Cloudflare 자격 · 인터넷 입구 없음).

## 처음 한 번 (자격 seed)

Cloudflare 토큰은 사람 계정에서만 만들 수 있으므로 이것만 bootstrap이다. 권한: 계정 `Cloudflare Tunnel` 편집,
`Access: Apps and Policies` 편집, zone `DNS` 편집. 값은 OpenBao `cloudflare-gitops-key-writer` 역할로 위 경로에 쓴다.
이후 회전도 같은 경로에 새 값을 쓰면 ESO(15분) → 조정기(다음 주기)로 반영된다.
