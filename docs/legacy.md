# 레거시와 현재 기준

2026-11-07 기준(develop `768a590d`). 옛 경로를 현재 절차로 읽지 않도록 한곳에 모은다. 이 목록에 없는 항목은 각 원문이 기준이다. `docs/evidence`의 실측·사고 기록은 레거시가 아니라 측정 당시의 기록이다.

## 현재 기준

| 범위 | 기준 |
|---|---|
| 목적지 설정·네트워크 경로 | [ADR-0032](adr/0032-destination-iac-api.md): `infra/iac/environments`(원하는 상태) → 생성 차트 `infra/iac/charts/<환경>` → 클러스터마다 Argo CD(`infra/iac/argocd`, 검토한 커밋 SHA 고정). 조회·계획 API는 `services/iac`(`/v1/iac/*`)이고 에이전트는 계획만 한다. |
| tailnet 경로 | `tailnet-egress`·`tailnet-ingress` 모듈 한곳. Tailscale, 클러스터 DNS, NetworkPolicy로 연결한다. |
| 플랫폼 서비스 배포 | [ADR-0029](adr/0029-platform-gitops-delivery.md): Helm과 T43 Argo CD, 이미지 digest 고정 |
| 계획 진행 | [ADR-0031](adr/0031-workflow-host.md): T10 워크플로 호스트 |
| 목적지 처음 구축 | ADR-0033(제안, #621) |
| 병합 | Merge Queue와 승인 조건이 없다. 필수 검사 3개가 통과하면 직접 병합한다([개발 규약](development.md)). |

## 레거시가 아닌 것

GitOps로 옮겨도 아래 보안·데이터 정책과 워크플로는 그대로다.

- 업무 쓰기 DB는 하나, 불명확하면 SAFE_HOLD, epoch 명령(T12). GATE 판정(T19), T10 승인 binding, diff·digest에 결속한 반영(T31, ADR-0021).
- 에이전트는 계획·제안만 하고 실행 권한이 없다. 도구 계층 차단(ADR-0019), 리허설 격리(ADR-0003·0025).
- 감사 로그(ADR-0020, T24). 사람·collector·리허설·apply 자격 분리. 인증 프록시와 noauth 프록시. broker guard·TLS 검증·실행 ledger.
- **대체 전까지 유일한 경로:** 플랫폼 API의 SSO → SSM 포트 포워딩 → 개인 인증서, 각 문서의 `kubectl port-forward` 절차, broker SSM 터널(`bravo-subscription-tunnel`). tailnet 사람 접근(T44)과 T38 대체가 들어온 뒤 한 PR에서 바꾼다.

## 레거시: 쓰지 않는다

| 옛 것 | 대체 | 근거 |
|---|---|---|
| `target-workloads/network.py ingress`·`egress` | `tailnet-ingress`·`tailnet-egress`(`source-dev`·`target-dev`) | #622. CLI는 종료 코드 2 |
| `executor-network/control_network.py routes`·`network`·`proxy-ingress`·`pooler-ingress` | `tailnet-egress`(`platform`), `tailnet-ingress` | #608·#622 |
| `collector-network/collector_route.py proxy-quarantine`·`routes`·`collector-egress`·`proxy-ingress` | `tailnet-egress` | #608 |
| `rehearsal-access/access.py platform`·`network`·`collector-platform`·`collector-network` | `tailnet-egress` | #608 |
| `workflow-host/network.py`의 T31 apply 경로(호스트 → apply egress proxy 규칙, `apply-proxy-ingress-workflow-host`) | `tailnet-egress` `bravo-apply-egress`의 client `platform-state/t10-workflow-host` | #631. 실환경의 옛 정책은 `infra/iac/charts/platform`이 적용된 뒤 지운다. |
| 위 렌더러 README의 경로 설명 절 | [`infra/iac/README.md`](../infra/iac/README.md) | 각 README 첫머리에 T44 표시가 있다. 함수는 점검 도구와 동치 시험용으로 남는다. |
| Merge Queue와 자동 큐 등록(T40·T41), [ADR-0017 develop merge queue](adr/0017-develop-merge-queue.md) | 필수 검사 뒤 직접 병합 | #616·#623. `MERGE_QUEUE_BOT_ENABLED` 변수는 `operations/rollout/run.py`가 읽으므로 남긴다. |
| GitHub Actions의 AI 제안 실행 | broker(T38) | 2026-11-06 제거, [`operations/subscription/README.md`](../operations/subscription/README.md) 첫머리 |
| #594(ADR-0032 infra-plan-api 초안) | ADR-0032 #592, ADR-0033 #621 | 2026-11-07 13:06 KST 정정. #594는 닫혔다. |
| 이미지 pin #609–#611 | #617–#619 | 닫혔다. |

## 레거시 후보: 대체를 인수한 뒤 지운다

실환경과 Git이 다르거나, 같은 자원에 주인이 둘이다. 대체를 확인하기 전에는 지우지 않는다.

| 지금 | 대체 | 담당 · 조건 |
|---|---|---|
| 목적지 S2 후보 수동 반입(`docs/operations/target-image-handoff.md`, `scripts/happy_path.py`의 `s2-candidate-import`) | 목적지 노드가 ECR에서 직접 pull. 실환경에는 2026-11-07 11:55 KST에 적용했고 Git에는 없다(`feature/T31-target-registry-pull`). | T31·T28. 그 브랜치를 병합한 뒤 반입 절차를 지운다. |
| T31 반영용 고정 토큰 kubeconfig Secret(8시간) | `infra/envs/rehearsal-access/credentials.py`의 exec 자격(600초 자동 발급) | T31·T7. 실제 만료를 넘긴 연속 조회와 회수 뒤 거부를 확인한 뒤 |
| `infra/envs/rehearsal-access/rehearsal-host-network.yaml`(수동 YAML, `bootstrap.py`가 읽음) | IaC 모듈 하나 | T15·T44. 실환경의 매핑 포트와 다르다. 적용된 정책을 모두 조사한 뒤 바꾼다. |
| 옛 고정 포트 NetworkPolicy(실환경 `t15-rehearsal-host-target-api`, `collector-target-api-client`, `cutover-executor-tailnet`, `apply-proxy-ingress-workflow-host`) | `infra/iac/charts/platform`의 NetworkPolicy | T44. workflow host apply 경로는 선언됐다. proxy UDP 포트를 선언하고 차트를 적용한 뒤 지운다. |
| `infra/iac/charts/platform`을 사람이 server-side apply(2026-11-07 13:37 KST) | T43 Argo Application 연결 | T44 할 일 |
| 롤아웃·호스트 설정·S1/S2 실행부가 개인 실행 폴더에만 있다 | 저장소의 정식 실행부 | T28 |
| 수동 토큰 교체·Secret 재배포 절차 | 자동 자격 경로 | 각 소비자. 인수 뒤 기본 안내에서 지운다. |

새 항목을 레거시로 옮길 때는 대체 경로가 병합·적용됐는지 원문에서 확인하고, 대체·근거·담당을 같은 표에 남긴다.
