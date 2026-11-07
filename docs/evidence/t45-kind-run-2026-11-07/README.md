# T45 목적지 처음 구축 실행기 — 로컬 kind 클러스터 실측

2026-11-07 13:17–13:38 KST(04:17–04:38Z)에 **이 노트북의 로컬 kind 클러스터**에서 `bravo-infra-plan run / confirm` CLI로 `target-onprem` 스택을 실제로 적용했다. **데모 목적지 RKE2가 아니며, 새 VM(`local-vm.sh`)도 아니다.** 이 노트북에 QEMU가 없어 kind를 썼다.

승인은 #603 브랜치의 실제 T10 코드(`approval_kind=infra` 승인 · 조회 · 소비 경로)를 SQLite 계획 저장소로 로컬에서 띄워 받았다. 신원은 SSO가 아니라 로컬 고정 토큰 두 개(승인자 `approver`, 실행자 `applier`)이고, 127.0.0.1에만 열었다. 계획 요청자는 `operator-jeon`으로 적었다.

## 환경

| 항목 | 값 |
|---|---|
| kind | v0.32.0, 노드 이미지 `kindest/node:v1.36.1@sha256:3489c767…f7ebd5`, 단일 노드 arm64 |
| 코드 | 실행기 브랜치(`feature/T44-infra-executor`, 이후 T45로 이름만 바꿈, 엔진 #604 `a04069d2` 위)의 작업 트리. T10은 #603 `a0c73422` |
| chart | 공식 저장소 아카이브 4개(스택의 고정 SHA-256과 일치), helm v4.3.1 |
| 설정 저장소 | kind 안의 Git 서버 Pod. 실행기는 127.0.0.1 port-forward로 fast-forward push, 목적지 Argo CD는 클러스터 내부 HTTP로 읽기 |
| 목적지 Argo CD | 당시 브랜치의 자체 chart(`infra/stacks/target-onprem/argocd`, argo-cd 10.14.0 Core, namespace `argocd`). **측정 뒤 T44와 겹쳐 지웠다.** 지금 코드는 T44 설치(`infra/iac/argocd`, 같은 argo-cd 10.14.0 Core, namespace `bravo-iac-gitops`)를 확인만 하므로, 아래 `argocd` 29.1초는 T45가 아니라 Argo CD 설치 자체의 시간이다 |

## 실행 순서와 결과

| 실행 | 무엇을 | 결과 |
|---|---|---|
| run-1 | 첫 시도 | 실측 결함 1 발견(아래). 프로세스를 중단했고 클러스터를 새로 만들었다 |
| run-2 | 새 클러스터, 결함 1 수정 | Argo CD 설치 29.1초, base 동기화 5.1초, ESO 동기화 101.3초(첫 이미지 pull 포함). CNPG 동기화가 602.9초 뒤 `HOLD`(`sync_timeout`) — 실측 결함 2 |
| run-3 | 결함 2 수정 뒤 새 승인 | 아래 표. DB · SeaweedFS · ESO 줄기 완료, OpenBao · Tailscale 줄기는 사람 단계 대기 |
| run-4 | 같은 스택 재실행 | Application의 기본값 필드 정리로 `base` · `eso` Application만 1회 재적용. 새 커밋 없음 |
| run-5 | 같은 스택 재실행 | 게시 단계 전부 `SKIPPED`, 새 커밋 · 새 Secret · helm 재설치 없음. 수렴 확인까지 약 3.2초 |
| run-6 | 승인 뒤 외부 커밋으로 설정 저장소 이동 | `REFUSED`(`plan_digest_mismatch`). 클러스터 · 저장소 변경 없음, T10 승인은 소비되지 않음(`claimed_run_id: null`) |
| — | 요청자와 같은 신원으로 T10 승인 | `409 self_approval_forbidden` |

### run-3 단계별 시간

`host-dirs` 확인(13:36:22 KST) 뒤 DB 마이그레이션 완료(13:37:18)까지 **56초**.

| 단계 | 상태 | 시간 |
|---|---|---|
| argocd · config-repo-access · db-secrets · object-secrets | `SKIPPED`(이미 수렴 · 관찰됨) | 0.1초 이하 |
| cnpg → cnpg-sync | `APPLIED` | 0.5초 → 15.2초 |
| database → database-sync → database-ready | `APPLIED` | 0.4초 → 50.7초 → 즉시 |
| migration → migration-complete | `APPLIED` | 0.1초 → 5.1초 |
| objects → objects-sync | `APPLIED` | 0.7초 → 30.4초 |
| openbao-image · openbao-tls · tailscale-oauth | `WAITING_OPERATOR` | — |

DB와 SeaweedFS 줄기는 같은 시각(13:36:22)에 함께 시작했다.

### 실제 클러스터 상태(run-3 뒤)

- CNPG `target-db`: `Cluster in healthy state`, 인스턴스 1
- 마이그레이션 Job `target-chat-schema-v1` 성공, `chat` DB에 `attachments` · `messages` 테이블
- `target-seaweedfs` Deployment 가용 1
- 실행기가 만든 `target-app-db` 비밀번호를 실행 기록 · 원장 · 설정 저장소 작업 폴더에서 검색: 0건

## 실측에서 찾아 고친 결함

1. **`default` AppProject 없음**: argo-cd chart는 서버를 끄면 `default` AppProject를 만들지 않는다. Application이 `InvalidSpecError`로 동기화되지 않았다. → 실행기가 스택 전용 AppProject(설정 저장소 URL로 제한)를 Application과 함께 적용한다.
2. **CNPG namespace 없음**: `helm template`은 release namespace를 만들지 않고, `cnpg-system`을 만드는 렌더러도 없다. → chart 렌더에 `create_namespace` 옵션을 추가하고 스택의 CNPG 렌더에 켰다(렌더러 코드는 그대로).
3. **Application 비교가 계속 다르게 나옴**: 매니페스트의 빈 값(`group: ""`, `recurse: false`)을 API 서버가 지워 server-side diff가 매번 차이를 보고했다. → 기본값 필드를 매니페스트에서 뺐다. run-4에서 한 번 정리된 뒤 run-5는 변경 없음.

## 범위 밖

- OpenBao(비공개 오프라인 이미지 필요), Tailscale(OAuth 자격 필요), 그 뒤의 delivery · app · verify 단계는 실행하지 않았다.
- T10은 SSO가 아닌 로컬 고정 토큰이다. 권한 검사(`approve_infra` · `apply_infra`)는 #603 단위 시험으로만 확인했다.
- 원장은 JSONL 파일이다(T24 연결 전).
- 측정 뒤 `kind delete cluster --name t44`로 클러스터를 지웠다.

---

# 2차 실측 — 결함 수정 뒤, T44 Argo CD와 실제 T10 검증기 · 권한으로

2026-11-07 15:40–16:00 KST(06:40–07:00Z), 새 kind 클러스터 `t45`(v1.36.1). 1차와 달리 다음을 실제 코드로 돌렸다.

| 항목 | 1차 | 2차 |
|---|---|---|
| 목적지 Argo CD | 자체 chart(지움) | **T44 `infra/iac/argocd`** — `infra/iac/README.md` 절차(설치 명령 2회, `environment=target-dev`, develop `768a590d`) |
| T10 신원 | 로컬 고정 토큰 → 고정 Principal | **실제 `OperatorTokenVerifier`**(RS256 · issuer · audience · client · acr · 수명 검사). IdP만 로컬 시험 키(팀 SSO 아님), 토큰 수명 120초 |
| T10 권한 | 모두 통과 | **실제 `OperatorAccess` 계획별 권한**: approver `read · approve_infra`, applier `read · apply_infra`, outsider `read` |
| 실행기 | 1차 코드 | 독립 리뷰 결함 수정 + `wait.stable_seconds` |

## 결과

| 확인 | 결과 |
|---|---|
| T44 Argo CD 설치(2회) | 40초. T45 `argocd` 단계는 설치를 관찰만 하고 `SKIPPED` |
| 토큰 없음 | T10 `403` |
| outsider(`read`만) · approver(`apply_infra` 없음)가 실행 | 둘 다 T10 `forbidden`으로 거부. 실행 기록 · 커밋 · Application 변경 없음, 승인 미소비 |
| applier 실행 run-1 | 실측 결함 4 발견(아래). 마이그레이션 Job 실패 → `migration-complete`가 600초 뒤 `HOLD`(`wait_timeout`). 자동 재시도 없이 멈췄다 |
| 수정 뒤 run-2 | 운영자가 실패한 Job을 손으로 지움(API에 삭제 없음) → 새 승인 → DB · SeaweedFS 줄기 완료. `database-ready` 20.3초(안정 시간), 마이그레이션 5.2초 성공(`attachments` · `messages`), SeaweedFS 동기화 25.4초. `host-dirs` 확인부터 마지막 완료까지 27.5초(DB가 run-1에서 이미 떠 있던 상태) |
| run-3 재실행 | 게시 단계 전부 `SKIPPED`, 새 커밋 없음. 24.8초(안정 시간 20초 포함) |

run-1의 첫 구축(빈 클러스터)에서 DB 동기화는 41초(06:43:41→06:44:22)였다.

## 실측에서 찾은 것

4. **DB Ready 직후 마이그레이션 연결 거부**: CNPG가 `Ready=True`가 된 같은 초(06:44:22)에 Job이 생성됐고 `target-db-rw` 연결이 거부됐다. Job은 렌더러 고정값 `backoffLimit: 0`이라 재시도하지 않는다. → `wait` 단계에 `stable_seconds`(조건이 그 시간 동안 계속 참)를 두고 `database-ready`에 20초. 실패한 Job은 사람이 지워야 다시 실행된다(삭제 동작 없음 원칙).
5. **T44와 Namespace 주인이 겹침(미해결)**: T44 생성 chart가 쓰는 namespace(`target-dev`, `tailscale-system`, `bravo-observability`)를 선언하고, T45 base도 `target-dev`를 낸다(T30 렌더러의 Pod Security `restricted` 레이블 포함). Argo CD가 "Namespace/target-dev is part of applications bravo-iac-target-dev and target-onprem-base"를 보고했다. 두 Application이 같은 field manager(`argocd-controller`)로 server-side apply하므로, T44가 동기화에 성공하면 마지막 적용 쪽 레이블만 남아 `restricted` 레이블이 지워질 수 있다. 주인을 하나로 정해야 한다(T44 담당과 합의 필요).
6. **T44 ProxyGroup은 Tailscale CRD가 필요**: T44 Application은 `ProxyGroup` CRD가 없어 동기화 실패 상태였다. CRD는 T45 `tailscale` 줄기(operator chart)가 설치한다. 두 작업 사이 순서 의존이다. 또 T44가 만드는 `tailscale-system` namespace에는 Pod Security 레이블이 없다(T45 사람 단계는 privileged로 만들도록 안내).

## 범위 밖(그대로)

OpenBao · Tailscale 줄기와 그 뒤 delivery · app · verify 단계, 팀 SSO(IAM Identity Center) 실토큰, RKE2 목적지. 측정 뒤 `kind delete cluster --name t45`로 지웠다.
