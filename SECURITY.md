# SOVA 보안

SOVA는 보안을 가장 먼저 내세우는 이전 플랫폼입니다. 고객이 클라우드를 떠나는 이유에는 주권과 규제, 보안이 포함되기 때문에, **옮긴 환경이 원본과 같은 보안 수준이라는 사실을 시험으로 확인하기 전에는 전환하지 않습니다.** 이 문서는 SOVA가 그 원칙을 어느 계층에서 어떻게 지키는지, 그리고 그중 무엇을 실제로 확인했는지 정리합니다.

이 문서는 develop `379f2ddc`(2026-11-08)를 기준으로 작성했습니다. 상태는 아래 세 단계로 표기하며, 근거가 부족하면 낮은 단계로 기록했습니다.

| 표기 | 의미 |
|---|---|
| **실환경** | 실제 AWS, 온프렘, GitHub에서 실행한 기록이 `docs/evidence/` 또는 PR 본문에 남아 있습니다 |
| **시험** | 로컬 환경이나 CI, 모의 환경에서 시험했습니다 |
| **설계** | 문서나 계약, 코드는 있지만 실제 경로에 연결되지 않았거나 실행 기록이 없습니다 |

## 계층 요약

| 계층 | 막으려는 위험 | 주 담당 | 상태 |
|---|---|---|---|
| 0. 전략 | Patching as a Service에서 착안한 "진단 → 수정 제안 → 격리 검증 → 승인 → 재진단" 순환을 이전 과정에 적용합니다 | 이상무 | 설계, 일부 시험 |
| 1. 저장소와 공급망 | 비밀 정보 유출, 변조된 이미지, 지나치게 넓은 CI 권한 | 팀 공통 | 실환경(CI) |
| 2. 신원과 접근 | 공유 자격 증명, 장기 토큰, 지나치게 넓은 네트워크 도달 범위 | 류지환, 이상무 | 실환경 일부 |
| 3. AI 에이전트 격리 | 모델이 직접 실행·판정·배포하는 상황, 모델 출력을 통한 유출과 파괴 | 류지환 | 실환경 일부(샌드박스 VM은 배포까지, Harbor 평가는 계획 단계) |
| 4. 리허설 격리 | 후보 코드가 운영 자원에 접근하는 상황 | 배규태, 류지환 | 실환경 |
| 5. 보안 동등성 시험과 게이트 | 원본에서 거부되던 접근이 목적지에서 허용되는 상황 | 이상무 | 실환경 일부(시험 3개). 실환경 GATE PASS는 0회 |
| 6. 전환 안전 | 업무 쓰기를 받는 DB가 둘이 되는 상황, 승인 없는 전환 | 배규태, 김태현, 류지환, 이상무 | Executor 실행은 실환경(데모 모드 1회), 거부와 복구 동작은 시험 |
| 7. 탐지와 대응 | 침해된 자산을 그대로 옮기는 상황 | 이상무 | 실환경 일부(GuardDuty) |
| 8. 거버넌스와 감사 | 판정·승인 기록의 삭제나 변조, 규제 대응 누락 | 이상무, 전동훈 | 실환경 일부(감사 기록 Object Lock, 출발지·목적지 감사 로그 검색) |

설계 원칙은 [에이전트 작업 안내](AGENTS.md#지켜야-할-설계-원칙)와 설계 문서 1장, 8.3절에 있습니다. 다섯 원칙은 다음과 같습니다.

- 결정적 워크플로가 흐름을 통제합니다.
- 판정 모듈과 실행 모듈에는 LLM을 사용하지 않습니다.
- 에이전트에게는 실행 권한을 주지 않습니다.
- 상태가 불명확하면 쓰기를 열지 않습니다.
- 보안 동등성은 시험 결과로 판단합니다.

## 0. 전략: Patching as a Service에서 착안한 순환

SoftBank와 SB OAI Japan은 2026-07-14에 OpenAI 기반 **Patching as a Service**를 일본 기업 3,000곳으로 확대하고 정식 출시한다고 발표했습니다([발표문](https://www.softbank.jp/en/corp/news/press/sbkk/2026/20260714_02/)). 발표문에 소개된 기능은 다음 네 가지입니다.

- 소스 코드 진단(Source code assessments)
- 공격 평가(Attack assessments)
- 보고서와 수정 권고(Reports and remediation recommendations)
- 패치 배포(Patch deployment)

이상무 님은 이 접근 방식을 **이전 과정**에 적용했습니다([Patching as a Service 기반 지속적 보안 개선](docs/security/continuous-secure-modernization.md)). SOVA는 먼저 이전 대상의 취약점과 노출 상태를 진단하고, AI가 수정안을 제안하면 격리된 리허설에서 "수정 전에는 FAIL, 수정 후에는 PASS"인지 확인합니다. 사람이 승인한 뒤에 전환하며, 전환 뒤에는 다시 진단합니다. 문서는 SoftBank의 서비스를 재현한다고 주장하지 않으며, 그 순환에서 **착안했다**는 범위를 명시합니다.

**Day-0 대응.** 이상무 님은 이 순환을 새로 공개된 취약점(Day-0)에 대응하는 방향으로 확장해 [PHP2Shell 대응 시나리오](docs/security/php2shell-ai-pentest-incident.md)를 설계했습니다. 이 시나리오는 php.net의 2026-10-21 권고(CVE-2026-109618/109619)를 대상으로 자산과 취약점, 노출, 침해 여부를 판정하고, 침해가 확인되면 재구축과 자격 증명 회전이 끝날 때까지 전환을 HOLD합니다. 다만 이 시나리오는 합성 fixture로 **시험**한 수준입니다. 또한 SoftBank 발표문에는 "Day-0"라는 표현이 없으므로, Day-0 대응은 팀의 해석으로 보아야 합니다.

판정 로직은 별도 공개 저장소인 [`Softbank-Hackathon-Finals/security`](https://github.com/Softbank-Hackathon-Finals/security)(Security Governance v1, 패키지 `sova-security`)에 있으며, 플랫폼은 이 저장소를 커밋 `cf33e09c`에 고정해 사용합니다. 남은 범위는 다음과 같습니다.

- AI 수정안 생성(SEC-PATCH-03)은 **설계** 단계입니다. 계약은 수정 범위만 검증하며, 모델 연결은 아직 남아 있습니다.
- 지속 재진단은 **시험** 단계입니다. 스케줄러와 수집기가 아직 연결되지 않았습니다.

## 1. 저장소와 공급망

| 통제 | 근거 | 상태 |
|---|---|---|
| PR과 push마다 비밀 정보 검사(gitleaks로 병합 커밋의 전체 조상 이력 검사)와 PR 제목 형식 검사를 실행합니다 | `.github/workflows/ci.yml`의 `secrets` · `scripts/ci_secrets.py` · `security-history.yml` | 실환경(CI) |
| Terraform, Dockerfile, 워크플로를 정적으로 검사하고 Trivy config와 fs 검사를 실행합니다 | `ci.yml`의 `infra`와 `platform` | 실환경(CI) |
| CI는 OIDC로만 권한을 받으며, 신뢰 조건은 변하지 않는 저장소 ID와 브랜치에 정확히 일치해야 합니다 | `infra/envs/executor-registry/main.tf:67,83` | 실환경 |
| 이미지 레지스트리의 태그는 덮어쓸 수 없고(IMMUTABLE), push할 때 스캔합니다 | `infra/envs/executor-registry/main.tf:59-62` · `infra/envs/private-registry/main.tf:15-21` | 실환경 |
| 이미지를 발행할 때 스캔하고 SBOM과 provenance를 붙이며, 배포는 digest를 고정하는 PR로만 진행합니다 | `.github/workflows/publish-*.yml` | 실환경(CI) |
| 검사 예외는 사유와 경로, 만료일을 적어 `.gitleaks.toml`과 `.trivyignore.yaml`에만 추가합니다 | [AGENTS.md 보안 규칙](AGENTS.md#보안-규칙) | 실환경(CI) |

## 2. 신원과 접근 (류지환, 이상무)

`infra/tailnet/routes-policy.json`의 경로 선언은 이상무 님의 목적지 IaC API(T44, [ADR-0032](docs/adr/0032-destination-iac-api.md))에서 시작했고, 류지환 님이 tailnet 정책 GitOps와 사람의 API 접근(`operator-access`)으로 넓혔습니다. 아래 표에서 운영자 API 거부와 네트워크 정책의 실환경 기록은 이상무 님이 측정했습니다.

| 통제 | 근거 | 상태 |
|---|---|---|
| 사람은 각자 IAM Identity Center로 로그인하고, 요청마다 60초 동안 유효한 STS 서명 증명을 만듭니다. 용도별 audience(`sova-mcp`, `bravo-platform-operator`, `bravo-platform-rehearsal`)는 서로 섞지 않습니다 | `services/console/desktop/src/main/proof.ts` · `services/sova-mcp/src/sova_mcp/access.py` | 실환경(T10 운영자 API에서, 등록되지 않은 운영자 신원의 계획 조회가 403으로 거부된 기록, `docs/evidence/t21-happy-path-intake-2026-11-08/`) |
| tailnet은 태그에서 태그로, 포트 단위로만 접근을 허용하며 와일드카드를 사용하지 않습니다 | `infra/tailnet/routes-policy.json` | 도달은 실환경, 거부는 시험 |
| 노드 메타데이터 서비스는 IMDSv2만 허용하고 hop 한도를 1로 두어, 컨테이너가 노드 역할을 사용하지 못하게 합니다 | `infra/envs/platform-aws/main.tf:105` 외 | 실환경 일부 |
| 플랫폼과 데이터 네임스페이스는 기본 거부 NetworkPolicy에서 시작합니다 | `infra/envs/platform-data/render.py` · `infra/charts/sova-mcp/templates/networkpolicy.yaml` | 실환경 일부(`t14-network-enforcement-2026-11-06`) |

## 3. AI 에이전트 격리 (류지환)

AI가 할 수 있는 작업은 **최초 제안서 한 건을 작성하는 것**뿐이며, 실행·판정·배포 권한은 없습니다([구독 런타임 안내](operations/subscription/README.md)). 제안서는 다음 경로로 처리됩니다.

1. 워크플로 호스트가 `bravo-initial` 네임스페이스에 Job을 만듭니다. 이 Job은 재시도 없이 최대 600초 동안만 실행됩니다.
2. Job은 노드의 loopback 주소에 있는 broker만 호출합니다. broker는 600초 동안 유효한 토큰을 확인한 뒤 모델을 호출합니다.
3. 제안서는 스키마와 정규식 검사를 거친 뒤 리허설(T15), 승인(T10), 반영(T31) 순서로 넘어갑니다.

| 통제 | 근거 | 상태 |
|---|---|---|
| 도구 허용 목록(5종)을 두고, 목록에 없는 도구 호출은 거부한 뒤 감사 기록에 남깁니다 | `services/agent-worker/src/bravo_agent/tools.py` | 실환경(`t11-subscription-2026-11-03`) |
| 응답 형태를 고정합니다. 턴마다 함수 호출은 정확히 1개이며, 병렬 호출과 호스티드 도구는 허용하지 않고, 호출에 묶이지 않은 항목이 섞이면 응답을 버립니다 | `openai_provider.py`(`FinalizedStreamCall`) | 실환경([#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)에서 PROPOSAL_READY, [#789](https://github.com/Softbank-Hackathon-Finals/platform/pull/789)에서 거부 쪽으로 닫힘) |
| 제안서 출력에 비밀 정보나 파괴 명령이 있는지 검사하고, 행동 기록을 남기지 못하면 제안서를 내보내지 않습니다 | `contracts.py` · `subscription_product.py` | 시험 |
| broker 프로세스는 systemd DynamicUser와 ProtectSystem strict로 실행되며, capability가 없고 메모리와 실행 시간에 상한이 있습니다 | `infra/envs/subscription-runtime/` | 실환경(t11) |
| STS broker는 전용 VPC 엔드포인트와 보조 주소에서 온 요청만 신뢰하고, nftables로 broker 프로세스만 통신하게 합니다 | `infra/envs/subscription-runtime/main.tf` · `broker-guard.sh.tftpl` | 실환경([#223](https://github.com/Softbank-Hackathon-Finals/platform/pull/223), [#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)) |
| Job 승인 정책(CEL)은 서비스 계정을 고정하고, 토큰 자동 마운트를 끄고, 이미지 digest를 요구하며, Secret 환경 변수와 포트를 금지하고, capability를 모두 제거하고, 루트 파일 시스템을 읽기 전용으로 둡니다 | `infra/gitops/argocd/templates/initial-proposal.yaml` | 설치와 실행은 실환경, 거부 동작은 렌더 시험 |
| **샌드박스 VM**: 셸 도구는 별도 VM 안의 `network_mode=none` 컨테이너에서만 실행합니다. VM은 Docker mTLS(플랫폼 노드 /32만 허용), userns-remap, no-new-privileges, icc 비활성화, 공인 IP 없음, IMDSv2 hop 1로 구성하며, 컨테이너에는 스냅숏 사본만 넣습니다 | `infra/envs/sandbox-session/` · `services/agent-worker/src/bravo_agent/sandbox_provider.py` | VM 배포는 실환경([#503](https://github.com/Softbank-Hackathon-Finals/platform/pull/503)). 컨테이너 실행은 로컬 Docker 시험까지 진행했고, 해피패스 설정에서는 아직 켜지 않았습니다 |
| 후보 이미지는 기준 이미지에 교체할 파일 계층만 덧붙여 만들며, 빌드 과정에서 후보 코드를 실행하지 않습니다 | `candidate_build.py` · [ADR-0052](docs/adr/0052-platform-candidate-build-without-github.md) | 실환경([#1007](https://github.com/Softbank-Hackathon-Finals/platform/pull/1007)) |
| 데스크톱 콘솔의 안내자는 셸, 앱, 플러그인, 브라우저, 개인 MCP, 네트워크, 웹 검색을 모두 끄고 SOVA MCP에만 연결하며, 쓰기 요청은 사람이 확인 창에서 눌러야 실행됩니다 | `services/console/desktop/src/main/agent.ts` | 설계(코드는 있으나 시험 기록 없음) |

**Harbor 평가(T26, 계획 단계).** SOVA는 런타임 격리와 평가를 분리합니다. 런타임 격리는 위에서 설명한 리허설 네임스페이스와 샌드박스 VM이 담당합니다. 에이전트의 수정 능력과 격리 수준은 Harbor로 수치화할 계획입니다. Harbor는 에이전트를 격리 환경에서 반복 실행하고 검증 스크립트로 결과를 채점하는 평가 프레임워크입니다. 계획은 열린 버킷, 시퀀스 불일치, 권한 누락 같은 결함을 심은 작업을 N회 반복해 수정 성공률과 차단 건수를 집계하는 것이며, 런타임과 같은 이미지와 검증 스크립트를 사용합니다([ADR-0015](docs/adr/0015-initial-design-decisions.md), [작업 T26](docs/tasks.md)). 현재는 행동 기록(trajectory) 원장까지만 구현했고, Harbor 작업 정의와 실행 기록은 아직 없습니다.

## 4. 리허설 격리

| 통제 | 근거 | 상태 |
|---|---|---|
| 리허설마다 무작위 이름의 네임스페이스를 만들고, PSA restricted와 기본 거부 정책을 적용합니다. SA 토큰은 주지 않고, NodePort와 LoadBalancer는 0개로 제한하며, UID 조건이 맞을 때만 삭제합니다 | `services/rehearsal-runner/` · [ADR-0002](docs/adr/0002-rehearsal-egress-allowlist.md) · [ADR-0003](docs/adr/0003-rehearsal-namespace-isolation.md) | 실환경(목적지 RKE2에서 필수 시험 14개 PASS, [#349](https://github.com/Softbank-Hackathon-Finals/platform/pull/349)) |
| 리허설 자격 증명은 저장된 제안에서만 선택하며, HTTP 입력으로는 자격 증명을 고를 수 없습니다 | `services/platform-events/src/bravo_platform_events/credential_host.py` | 시험 |

## 5. 보안 동등성 시험과 원본 대조 게이트 (이상무)

이 계층은 SOVA의 중심입니다. SOVA는 **같은 시험을 출발지와 목적지에서 똑같이 실행하고, 결과가 다르거나 확인할 수 없으면 전환을 막습니다.** 제품 이름이 같은지를 보는 것이 아니라, 원본에서 거부되던 접근이 목적지에서도 실제로 거부되는지를 확인합니다.

| 통제 | 근거 | 상태 |
|---|---|---|
| 통제 6개(설계 8.5절)를 하위 시험 20개로 나누어 실행합니다(접두어별로 IAM 2, SEC 4, ENC 6, AUD 3, NET 3, ISO 1, DR 1) | [통제 계약](docs/controls/README.md) · `services/control-runner/` | 시험. 전체 프로필 `full:1`의 실환경 결과는 PASS 1개, UNKNOWN 19개였습니다 |
| 데모 프로필 `demo-gate:1`(IAM-01.1, SEC-01.1, AUD-01.1)을 출발지 EKS와 목적지 RKE2에서 실행했습니다 | [T14 GATE 기록용 실행](docs/evidence/t14-gate-record-2026-11-08/README.md) | **실환경**. 목적지의 IAM-01.1 FAIL은 의도적으로 심은 결함이며, 게이트는 의도대로 BLOCK했습니다 |
| 게이트는 PASS가 아니면 BLOCK하고, UNKNOWN도 BLOCK으로 처리하며, 판정 결과에 서명해 기록합니다 | `services/control-gate/` | 실환경 판정 기록이 있습니다. **실환경 GATE PASS는 0회**입니다 |
| 한국과 일본의 규제 조항을 통제와 연결한 대응표를 둡니다 | [규제 대응](docs/controls/regulatory-mapping.md) | 설계 |

## 6. 전환 안전

| 통제 | 근거 | 상태 |
|---|---|---|
| Cutover Executor는 자원별 epoch를 단조 증가시키고, lease를 하나만 허용하며, 원격 호출 직전마다 소유권을 다시 확인합니다. 원격 호출 전에 의도를 먼저 기록하고, 결과를 성공·실패·확인 불가로 구분하며, 커밋 이후에 실패하면 사람이 해제할 때까지 SAFE_HOLD로 멈춥니다 | `services/cutover-executor/` · [ADR-0011](docs/adr/0011-cutover-executor-execution-and-failure.md) | 실환경(데모 모드 계획에서 9단계 DONE, [#1048](https://github.com/Softbank-Hackathon-Finals/platform/pull/1048)). 거부 동작은 시험 |
| 승인은 plan_digest와 일치해야 하고, 만료 시각이 있으며, 한 번만 소비됩니다. 오래된 승인이나 이미 사용한 epoch는 거부합니다 | `services/platform-events/src/bravo_platform_events/cutover.py` | 시험 |
| Edge 가중치는 0·10·50·100%만 허용하며, SHIFT 상태가 아니면 0%만 허용합니다 | `services/edge-control/` | 시험(실제 Edge에서는 데모 중 0%에서 100%로 바꾼 기록만 있음) |
| 업무 쓰기를 받는 DB는 어느 순간에도 최대 하나이며, 상태를 확인할 수 없으면 쓰기를 열지 않습니다 | 설계 문서 8.3절 · Executor 펜스 확인 | 시험 |
| SHIFT와 WATCH는 오류율과 p95의 창 요약으로 판정합니다. 가중치를 바꾼 직후 첫 창이 도착하기 전의 UNKNOWN은 연속 UNKNOWN 수에 넣지 않고 HOLD로 둡니다(T36, 이상무) | [판정용 관측 계약](docs/observability/README.md) · `services/observability/` · [#566](https://github.com/Softbank-Hackathon-Finals/platform/pull/566) | 시험(1,600개 조합 전수 실행) |
| CNPG 백업을 새 UID의 클러스터로 복원한 뒤 SQL로 표식을 읽어 백업 시점과 대조합니다. 최초 복구에 필요한 WAL이 없으면 복원이 진행되지 않고 원본은 그대로 남습니다(T35, 이상무) | `docs/evidence/t35-restore-readback-2026-11-04/` · `docs/evidence/t35-wal-loss-2026-11-04/` | 시험(로컬 kind와 CNPG) |

## 7. 탐지와 대응 (이상무)

| 통제 | 근거 | 상태 |
|---|---|---|
| GuardDuty, Security Hub, GCP SCC, Falco, Kyverno의 결과를 OCSF 부분집합으로 정규화하며, 수집에 실패하면 0건이 아니라 "확인 불가"로 표시합니다 | [탐지 통합](docs/security/detection-integration.md) · `services/security-findings/` | 시험 |
| GuardDuty의 실제 탐지 결과를 수집했습니다 | `docs/evidence/t33-guardduty-live-2026-11-08/` · [#903](https://github.com/Softbank-Hackathon-Finals/platform/pull/903) | **실환경**(실계정 샘플 탐지) |
| Security Hub 수집을 시도했습니다 | `docs/evidence/t33-securityhub-2026-11-06/` | 접근 거부 상태만 확인했습니다 |
| 플랫폼에서 GuardDuty와 SCC를 주기적으로 수집하는 CronJob과 Falco, Kyverno 연동을 준비했습니다 | [#909](https://github.com/Softbank-Hackathon-Finals/platform/pull/909), [#946](https://github.com/Softbank-Hackathon-Finals/platform/pull/946) | 설계(기본값은 꺼짐) |
| 침해가 확인된 자산은 재구축과 자격 증명 회전이 끝날 때까지 HOLD합니다 | [PHP2Shell 시나리오](docs/security/php2shell-ai-pentest-incident.md) | 시험(합성 fixture) |

## 8. 거버넌스와 감사

| 통제 | 근거 | 상태 |
|---|---|---|
| 감사 버킷은 S3 Object Lock COMPLIANCE 모드로 운영하며, 삭제와 보존 기간 단축을 모두 거부합니다(T24, 전동훈) | `infra/envs/platform-audit/` | **실환경**(`docs/evidence/t24-audit-live-2026-11-06/`) |
| 출발지와 목적지에서 DB 관리자 로그인·DDL과 Kubernetes 권한 변경의 감사 기록을 수집하고 검색합니다(T34, 이상무) | `docs/evidence/t34-source-db-audit-2026-11-05/` · `docs/evidence/t34-source-eks-audit-2026-11-06/` · `docs/evidence/t34-target-aud-pod-log-2026-11-07/` | 실환경 일부(원천 로그 검색까지 확인했습니다. 중앙 수집과 검색 경로 전체는 인수하지 못했습니다) |
| Security Review는 기존 변경 승인과 컷오버 승인에 추가되는 fail-closed 조건이며, `execution_authorized=false`이면 진행을 멈춥니다 | [플랫폼 보안 거버넌스](docs/security/platform-security-governance.md) · [#924](https://github.com/Softbank-Hackathon-Finals/platform/pull/924) | 시험 |
| 국내외 보안 사고에서 통제를 도출한 사고 기반 위협 모델을 둡니다 | [사고 기반 아키텍처](docs/security/incident-based-architecture.md) | 설계 |

## 알려진 한계

SOVA는 보안을 앞세우는 만큼, 아직 지키지 못한 부분도 숨기지 않습니다. 자세한 경위는 [회고와 한계](docs/retrospective.md)에 있습니다.

1. **실환경에서 GATE PASS, 전환, 감시를 한 번도 연속으로 완료하지 못했습니다.** 최종 데모는 리허설과 게이트, 감시를 건너뛰는 데모 모드로 진행했습니다.
2. **설정만으로 사람의 승인 게이트를 우회할 수 있습니다.** 코드에는 데모 모드(`demo.skip`, `operator.json`의 `demo`), 자동 위임(`delegation.auto_approver`), 보안 검토 비활성화(`security_review: false`) 설정이 남아 있습니다. 데모 모드는 건너뛴 단계를 PASS 형태의 증빙으로 기록합니다. 데모에서 보안 검토가 켜져 있었는지는 확인하지 않았으므로, 컷오버가 Security Review를 통과했다고 주장하지 않습니다.
3. **Executor는 승인을 직접 검증하지 않습니다.** Executor는 명령 토큰과 epoch만 확인하며, 승인 여부는 T10에서만 확인합니다.
4. **실행 권한이 한곳에 모여 있습니다.** 리허설 호스트 Pod 하나에 컷오버 명령 토큰과 Edge 토큰이 함께 있으며, 점검 문서에는 이를 해피패스 기간에 한정한 위험으로 기록했습니다.
5. **에이전트 경로에 남은 틈이 있습니다.**
   - `bravo-initial`은 PSA privileged이므로, 승인 정책이 Pod를 제한하는 유일한 경계입니다.
   - 모델 워커의 송신 대상을 LLM API로만 제한하는 규칙이 없습니다.
   - 샌드박스 컨테이너에는 pids와 메모리 한도가 없습니다.
   - 샌드박스 VM 역할에는 다른 EC2 역할에 있는 SSM 파라미터 읽기 거부 규칙이 없습니다.
   - 파괴 명령 차단은 알려진 패턴만 확인합니다.
6. **호스트 설정이 Git 밖의 ConfigMap에 있습니다.** 따라서 저장소만 보고는 배포된 보안 동작을 확정할 수 없습니다.
7. **일부 인용 수치는 원문으로 확인하지 않았습니다.** `docs/security/`에 인용된 사고 피해 규모 등이 이에 해당하며, 발표나 외부 문서에 사용하기 전에는 원문을 확인해야 합니다(`docs/security/README.md`).

## 관련 문서

- [보안 문서 목차](docs/security/README.md): 원본은 노션(팀원 전용)에 있고, 이 저장소에는 사본이 있습니다.
- [통제 계약](docs/controls/README.md), [규제 대응](docs/controls/regulatory-mapping.md)
- [Security Governance v1](https://github.com/Softbank-Hackathon-Finals/security)
- [공개 증빙 절차](docs/public-evidence-policy.md): 공개 문서에서 가리는 값과 원본을 보존하는 방법

## 취약점 신고

공개 이슈나 PR에는 재현 절차나 비밀 값을 올리지 말아 주세요. 대신 저장소 관리자([CODEOWNERS](.github/CODEOWNERS))에게 비공개로 알려 주시고, 영향을 받는 경로와 커밋을 함께 적어 주세요. 본선이 끝났으므로 운영 자원은 정리 대상입니다.
