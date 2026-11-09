# SOVA 보안

SOVA는 보안을 앞세운 이전 플랫폼이다. 고객이 클라우드를 떠나는 이유가 주권·규제·보안이기 때문에, **옮긴 환경이 원본과 같은 보안 수준이라는 것을 시험으로 보이기 전에는 전환하지 않는다.** 이 문서는 그 약속을 어떤 계층에서 어떻게 지키는지, 그리고 그중 무엇이 실제로 확인됐는지를 정리한다.

기준은 develop `379f2ddc`(2026-11-08)이다. 상태는 세 단계로 표기한다. 근거가 없으면 낮은 쪽으로 적었다.

| 표기 | 뜻 |
|---|---|
| **실환경** | 실제 AWS·온프렘·GitHub에서 실행한 기록이 `docs/evidence/` 또는 PR 본문에 있다 |
| **시험** | 로컬·CI·모의 환경 시험까지 했다 |
| **설계** | 문서·계약·코드는 있지만 연결되지 않았거나 실행 기록이 없다 |

## 한눈에 보기

| 계층 | 무엇을 막나 | 주 담당 | 상태 |
|---|---|---|---|
| 0. 전략 | Patching as a Service에서 착안한 "진단 → 수정 제안 → 격리 검증 → 승인 → 재진단" 순환을 이전에 적용 | 이상무 | 설계·일부 시험 |
| 1. 저장소·공급망 | 비밀 유출, 변조된 이미지, 넓은 CI 권한 | 팀 공통 | 실환경(CI) |
| 2. 신원·접근 | 공유 자격, 장기 토큰, 넓은 네트워크 도달 | 류지환 | 실환경 일부 |
| 3. AI 에이전트 격리 | 모델이 실행·판정·배포를 하는 것, 모델 출력의 유출·파괴 | 류지환 | 실환경 일부 · 샌드박스 VM은 배포만 · Harbor는 계획 |
| 4. 리허설 격리 | 후보 코드가 운영 자원에 닿는 것 | 배규태 · 류지환 | 실환경 |
| 5. 보안 동등성 시험·게이트 | 원본에서 거부되던 접근이 목적지에서 허용되는 것 | 이상무 | 실환경 일부(시험 3개). 실환경 GATE PASS는 0회 |
| 6. 전환 안전 | 쓰기를 받는 DB가 둘이 되는 것, 승인 없는 전환 | 배규태 · 김태현 · 류지환 | Executor 실행은 실환경(데모 모드 1회), 거부·복구는 시험 |
| 7. 탐지·대응 | 침해된 자산을 그대로 옮기는 것 | 이상무 | 실환경 일부(GuardDuty) |
| 8. 거버넌스·감사 | 판정·승인 기록의 삭제·변조, 규제 대응 누락 | 이상무 · 전동훈 | 실환경 일부(감사 Object Lock) |

설계 원칙은 [에이전트 작업 안내](AGENTS.md#지켜야-할-설계-원칙)와 설계 문서 1장·8.3에 있다.
- 결정적 워크플로가 흐름을 쥔다.
- 판정·실행 모듈에 LLM을 쓰지 않는다.
- 에이전트에게 실행 권한을 주지 않는다.
- 불명확하면 쓰기를 열지 않는다.
- 동등성은 시험 결과로 판단한다.

## 0. 전략: Patching as a Service에서 착안

SoftBank와 SB OAI Japan은 2026-07-14 OpenAI 기반 **Patching as a Service**를 일본 기업 3,000곳으로 확대하고 정식 출시한다고 발표했다([발표문](https://www.softbank.jp/en/corp/news/press/sbkk/2026/20260714_02/)). 발표문의 네 기능은 다음과 같다.
- 소스 코드 진단(Source code assessments)
- 공격 평가(Attack assessments)
- 보고서와 수정 권고(Reports and remediation recommendations)
- 패치 배포(Patch deployment)

이상무 님은 이 접근을 **이전 과정**에 옮겼다([Patching as a Service 기반 지속적 보안 개선](docs/security/continuous-secure-modernization.md)).
- 이전 대상의 취약점·노출을 진단한다.
- AI는 수정안만 제안한다.
- 격리된 리허설에서 "수정 전 FAIL, 수정 후 PASS"를 확인한다.
- 사람이 승인한다.
- 전환 뒤 다시 진단한다.
- 같은 서비스를 재현하는 것이 아니라 그 순환에서 **착안**했다는 것이 문서의 명시적 범위다.

**Day-0 대응.** 팀은 이 순환을 막 공개된 취약점(Day-0)에 대응하는 데까지 확장해 설계했다. 예시는 [PHP2Shell 대응 시나리오](docs/security/php2shell-ai-pentest-incident.md)다.
- php.net 2026-10-21 권고(CVE-2026-109618/109619)가 대상이다.
- 자산·취약점·노출·침해를 판정한다.
- 침해가 확인되면 재구축과 자격 회전 전까지 전환을 HOLD한다.
- 이 시나리오는 합성 fixture로 **시험**한 수준이다.
- SoftBank 발표문에는 "Day-0"라는 표현이 없다. 이는 팀의 해석이다.

판정 로직은 별도 공개 저장소 [`Softbank-Hackathon-Finals/security`](https://github.com/Softbank-Hackathon-Finals/security)(Security Governance v1, `sova-security`)에 있다. 플랫폼은 이 저장소를 `cf33e09c`에 고정해 쓴다. 남은 범위는 다음과 같다.
- AI 수정안 생성(SEC-PATCH-03): **설계**. 계약은 범위만 검증하고, 모델 연결이 남았다.
- 지속 재진단: **시험**. 스케줄러와 수집기가 연결되지 않았다.

## 1. 저장소·공급망

| 통제 | 근거 | 상태 |
|---|---|---|
| PR·push마다 비밀 검사(gitleaks, 병합 커밋의 전체 조상 이력), PR 제목 형식 | `.github/workflows/ci.yml` `secrets` · `scripts/ci_secrets.py` · `security-history.yml` | 실환경(CI) |
| Terraform·Dockerfile·워크플로 정적 검사, Trivy config·fs | `ci.yml` `infra`·`platform` | 실환경(CI) |
| CI 권한은 OIDC로만 받는다. 신뢰 조건은 바뀌지 않는 저장소 ID와 브랜치로 정확히 일치시킨다 | `infra/envs/executor-registry/main.tf:67,83` | 실환경 |
| 이미지 레지스트리 태그 불변(IMMUTABLE), push 시 스캔 | `infra/envs/executor-registry/main.tf:59-62` · `infra/envs/private-registry/main.tf:15-21` | 실환경 |
| 이미지 발행 시 스캔·SBOM·provenance, 배포는 digest 고정 PR로만 | `.github/workflows/publish-*.yml` | 실환경(CI) |
| 예외는 사유·경로·만료와 함께 `.gitleaks.toml`·`.trivyignore.yaml`에만 둔다 | [AGENTS.md 보안 규칙](AGENTS.md#보안-규칙) | 실환경(CI) |

## 2. 신원·접근

| 통제 | 근거 | 상태 |
|---|---|---|
| 사람은 각자 IAM Identity Center로 로그인한다. 요청마다 60초짜리 STS 서명 증명을 만들고, 용도별 audience(`sova-mcp`, `bravo-platform-operator`, `bravo-platform-rehearsal`)를 섞지 않는다 | `services/console/desktop/src/main/proof.ts` · `services/sova-mcp/src/sova_mcp/access.py` | 실환경(권한 없는 계획 조회가 403으로 거부됨, `docs/evidence/t21-happy-path-intake-2026-11-08/`) |
| tailnet은 태그 → 태그·포트 단위로만 허용하고 와일드카드가 없다 | `infra/tailnet/routes-policy.json` | 도달은 실환경, 거부는 시험 |
| 노드 메타데이터는 IMDSv2만 받고 hop 한도 1이라, 컨테이너가 노드 역할을 쓰지 못한다 | `infra/envs/platform-aws/main.tf:105` 외 | 실환경 일부 |
| 플랫폼·데이터 네임스페이스는 기본 거부 NetworkPolicy에서 출발한다 | `infra/envs/platform-data/render.py` · `infra/charts/sova-mcp/templates/networkpolicy.yaml` | 실환경 일부(`t14-network-enforcement-2026-11-06`) |

## 3. AI 에이전트 격리 (류지환)

AI는 **최초 제안서 한 건을 쓰는 것**만 할 수 있다. 실행·판정·배포 권한은 없다([구독 런타임 안내](operations/subscription/README.md)). 경로는 다음과 같다.
- 워크플로 호스트가 `bravo-initial`에 Job을 만든다. Job은 재시도 0회, 최대 600초다.
- Job은 노드 loopback의 broker만 부른다. broker는 600초 토큰을 확인한 뒤 모델을 호출한다.
- 제안서는 스키마·정규식 검사를 거쳐 리허설(T15) → 승인(T10) → 반영(T31)으로 간다.

| 통제 | 근거 | 상태 |
|---|---|---|
| 도구 허용 목록(5종). 목록 밖 도구는 거부하고 감사에 남긴다 | `services/agent-worker/src/bravo_agent/tools.py` | 실환경(`t11-subscription-2026-11-03`) |
| 응답 형태 고정: 턴당 함수 호출 정확히 1개, 병렬 호출 끔, 호스티드 도구 거부. 묶이지 않은 항목이 있으면 버린다 | `openai_provider.py`(`FinalizedStreamCall`) | 실환경([#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588) PROPOSAL_READY, [#789](https://github.com/Softbank-Hackathon-Finals/platform/pull/789) 거부 쪽으로 닫힘) |
| 제안서 출력의 비밀·파괴 명령 검사, 행동 기록 실패 시 제안서를 내보내지 않음 | `contracts.py` · `subscription_product.py` | 시험 |
| broker 프로세스: systemd DynamicUser, ProtectSystem strict, capability 없음, 메모리·시간 상한 | `infra/envs/subscription-runtime/` | 실환경(t11) |
| STS broker: 전용 VPC 엔드포인트와 보조 주소에서만 신뢰, nftables로 broker만 허용 | `infra/envs/subscription-runtime/main.tf` · `broker-guard.sh.tftpl` | 실환경([#223](https://github.com/Softbank-Hackathon-Finals/platform/pull/223), [#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)) |
| Job 승인 정책(CEL): SA 고정, 토큰 자동 마운트 끔, 이미지 digest, Secret env·포트 금지, capability drop ALL, 읽기 전용 루트 | `infra/gitops/argocd/templates/initial-proposal.yaml` | 설치·실행은 실환경, 거부 동작은 렌더 시험 |
| **샌드박스 VM**: 셸 도구는 별도 VM의 `network_mode=none` 컨테이너에서만 실행. Docker mTLS(플랫폼 노드 /32), userns-remap, no-new-privileges, icc 끔, 공인 IP 없음, IMDSv2 hop 1. 컨테이너에는 스냅숏 사본만 넣는다 | `infra/envs/sandbox-session/` · `services/agent-worker/src/bravo_agent/sandbox_provider.py` | VM 배포는 실환경([#503](https://github.com/Softbank-Hackathon-Finals/platform/pull/503)). 컨테이너 실행은 로컬 Docker 시험. 해피패스 설정에는 아직 켜지 않았다 |
| 후보 이미지는 기준 이미지에 교체 파일 계층만 덧붙인다. 후보 코드를 빌드 중에 실행하지 않는다 | `candidate_build.py` · [ADR-0052](docs/adr/0052-platform-candidate-build-without-github.md) | 실환경([#1007](https://github.com/Softbank-Hackathon-Finals/platform/pull/1007)) |
| 데스크톱 콘솔 안내자: 셸·앱·플러그인·브라우저·개인 MCP 끔, 네트워크·웹 검색 끔, SOVA MCP만 연결, 쓰기는 사람 클릭 확인 | `services/console/desktop/src/main/agent.ts` | 설계(코드만, 시험 기록 없음) |

**Harbor 평가 (T26, 계획).** 런타임 격리와 평가를 나눈다.
- 런타임은 위의 리허설 네임스페이스와 샌드박스 VM이 맡는다.
- 에이전트의 수정 능력과 격리는 Harbor(에이전트를 격리 환경에서 반복 실행하고 검증 스크립트로 채점하는 평가 프레임워크)로 수치화한다.
- 결함을 심은 작업을 N회 반복해 수정 성공률과 차단 건수를 집계한다. 예: 열린 버킷, 시퀀스 불일치, 권한 누락.
- 런타임과 같은 이미지·검증 스크립트를 쓴다([ADR-0015](docs/adr/0015-initial-design-decisions.md), [작업 T26](docs/tasks.md)).
- 지금은 행동 기록(trajectory) 원장까지 들어갔다. Harbor 작업 정의와 실행 기록은 아직 없다.

## 4. 리허설 격리

| 통제 | 근거 | 상태 |
|---|---|---|
| 리허설마다 무작위 이름 네임스페이스. PSA restricted, 기본 거부, SA 토큰 없음, NodePort·LoadBalancer 0개, UID 조건으로만 삭제 | `services/rehearsal-runner/` · [ADR-0002](docs/adr/0002-rehearsal-egress-allowlist.md) · [ADR-0003](docs/adr/0003-rehearsal-namespace-isolation.md) | 실환경(목적지 RKE2, 필수 14개 PASS, [#349](https://github.com/Softbank-Hackathon-Finals/platform/pull/349)) |
| 리허설 자격은 저장된 제안에서만 고른다. HTTP 입력으로 자격을 선택할 수 없다 | `services/platform-events/src/bravo_platform_events/credential_host.py` | 시험 |

## 5. 보안 동등성 시험과 원본 대조 게이트 (이상무)

SOVA의 중심 계층이다. **같은 시험을 출발지와 목적지에 똑같이 돌리고, 답이 다르거나 확인할 수 없으면 전환을 막는다.** 제품 이름이 같다는 이유가 아니라, 원본에서 거부되던 접근이 목적지에서도 실제로 거부되는지를 본다.

| 통제 | 근거 | 상태 |
|---|---|---|
| 통제 6개(설계 8.5), 하위 시험 20개(접두어별 IAM 2, SEC 4, ENC 6, AUD 3, NET 3, ISO 1, DR 1) | [통제 계약](docs/controls/README.md) · `services/control-runner/` | 시험. 전체 프로필 `full:1`은 실환경에서 PASS 1, UNKNOWN 19 |
| 데모 프로필 `demo-gate:1`(IAM-01.1, SEC-01.1, AUD-01.1)을 출발지 EKS·목적지 RKE2에서 실행 | [T14 GATE 기록용 실행](docs/evidence/t14-gate-record-2026-11-08/README.md) | **실환경**. 목적지 IAM-01.1 FAIL은 심어 둔 결함이고, 게이트는 의도대로 BLOCK했다 |
| 게이트: PASS가 아니면 BLOCK. UNKNOWN도 BLOCK. 판정 결과는 서명해 기록한다 | `services/control-gate/` | 실환경 판정 기록 있음. **실환경 GATE PASS는 0회** |
| 한·일 규제 조항 대응표 | [규제 대응](docs/controls/regulatory-mapping.md) | 설계 |

## 6. 전환 안전

| 통제 | 근거 | 상태 |
|---|---|---|
| Cutover Executor: 자원별 epoch 단조 증가, lease 1개, 원격 호출 직전마다 소유권 확인, 의도를 먼저 기록, 성공·실패·확인 못 함 구분, 커밋 뒤 실패는 SAFE_HOLD(사람만 해제) | `services/cutover-executor/` · [ADR-0011](docs/adr/0011-cutover-executor-execution-and-failure.md) | 실환경(데모 모드 계획에서 9단계 DONE, [#1048](https://github.com/Softbank-Hackathon-Finals/platform/pull/1048)). 거부 동작은 시험 |
| 승인 결속: plan_digest 일치, 만료, 1회 소비, 오래된 승인·epoch 재사용 거부 | `services/platform-events/src/bravo_platform_events/cutover.py` | 시험 |
| Edge 가중치는 0·10·50·100%만 허용하고, SHIFT 상태가 아니면 0만 허용 | `services/edge-control/` | 시험(실제 Edge는 데모에서 0 → 100%만) |
| 어느 순간에도 업무 쓰기를 받는 DB는 최대 하나. 확인할 수 없으면 쓰기를 열지 않는다 | 설계 문서 8.3 · Executor 펜스 확인 | 시험 |

## 7. 탐지·대응 (이상무)

| 통제 | 근거 | 상태 |
|---|---|---|
| GuardDuty·Security Hub·GCP SCC·Falco·Kyverno 결과를 OCSF 부분집합으로 정규화한다. 수집 실패는 0건이 아니라 "확인 불가"로 표시한다 | [탐지 통합](docs/security/detection-integration.md) · `services/security-findings/` | 시험 |
| GuardDuty 실제 탐지 수집 | `docs/evidence/t33-guardduty-live-2026-11-08/` · [#903](https://github.com/Softbank-Hackathon-Finals/platform/pull/903) | **실환경**(실계정 샘플 탐지) |
| Security Hub | `docs/evidence/t33-securityhub-2026-11-06/` | 접근 거부 상태만 확인 |
| 플랫폼 주기 수집(GuardDuty·SCC CronJob), Falco·Kyverno | [#909](https://github.com/Softbank-Hackathon-Finals/platform/pull/909), [#946](https://github.com/Softbank-Hackathon-Finals/platform/pull/946) | 설계(기본 꺼짐) |
| 침해가 확인된 자산은 재구축·자격 회전 전까지 HOLD | [PHP2Shell 시나리오](docs/security/php2shell-ai-pentest-incident.md) | 시험(합성 fixture) |

## 8. 거버넌스·감사

| 통제 | 근거 | 상태 |
|---|---|---|
| 감사 버킷 S3 Object Lock COMPLIANCE. 삭제·보존 단축 모두 거부 | `infra/envs/platform-audit/` | **실환경**(`docs/evidence/t24-audit-live-2026-11-06/`) |
| Security Review: 기존 변경·컷오버 승인에 붙는 추가 fail-closed 조건(`execution_authorized=false`면 멈춤) | [플랫폼 보안 거버넌스](docs/security/platform-security-governance.md) · [#924](https://github.com/Softbank-Hackathon-Finals/platform/pull/924) | 시험 |
| 사고 기반 위협 모델(국내외 사고에서 통제 도출) | [사고 기반 아키텍처](docs/security/incident-based-architecture.md) | 설계 |

## 알려진 한계

보안을 앞세운 만큼, 아직 지키지 못한 것도 숨기지 않는다. 자세한 경위는 [회고와 한계](docs/retrospective.md)에 있다.

1. **실환경에서 GATE PASS → 전환 → 감시를 한 번도 잇지 못했다.** 최종 데모는 리허설·게이트·감시를 건너뛰는 데모 모드로 진행했다.
2. **설정으로 사람 게이트를 우회할 수 있다.** 다음 설정이 코드에 남아 있다.
   - 데모 모드(`demo.skip`, `operator.json`의 `demo`). 건너뛴 단계를 PASS형 증빙으로 남긴다.
   - 자동 위임(`delegation.auto_approver`)
   - 보안 검토 끄기(`security_review: false`)
   - 데모에서 보안 검토가 켜져 있었는지는 확인하지 않았다. 따라서 "컷오버가 Security Review를 통과했다"고 주장하지 않는다.
3. **Executor는 승인을 직접 검증하지 않는다.** 명령 토큰과 epoch만 본다. 승인 확인은 T10에서만 한다.
4. **실행 권한이 한 곳에 모였다.** 리허설 호스트 Pod 하나에 컷오버 명령 토큰과 Edge 토큰이 함께 있다. 점검 문서에 해피패스 한정 위험으로 적었다.
5. **에이전트 경로의 남은 틈.**
   - `bravo-initial`은 PSA privileged이고, 승인 정책이 유일한 Pod 경계다.
   - 모델 워커의 송신을 LLM API로만 제한하는 규칙이 없다.
   - 샌드박스 컨테이너에 pids·메모리 한도가 없다.
   - 샌드박스 VM 역할에는 다른 EC2 역할에 있는 SSM 파라미터 읽기 거부가 없다.
   - 파괴 명령 차단은 알려진 패턴만 본다.
6. **호스트 설정이 Git 밖의 ConfigMap에 있다.** 그래서 배포된 보안 동작을 저장소만 보고는 확정할 수 없다.
7. **일부 인용 수치는 원문으로 확인하지 않았다.** `docs/security/`의 사고 피해 규모 등이 그렇다. 발표·외부 문서에 쓰기 전에 원문을 연다(`docs/security/README.md`).

## 문서 지도

- [보안 문서 목차](docs/security/README.md): 원본은 노션(팀원 전용)이고, 이 저장소에는 사본이 있다.
- [통제 계약](docs/controls/README.md), [규제 대응](docs/controls/regulatory-mapping.md)
- [Security Governance v1](https://github.com/Softbank-Hackathon-Finals/security)
- [공개 증빙 절차](docs/public-evidence-policy.md): 공개 문서에서 가리는 값과 원본 보존 방법

## 취약점 신고

공개 이슈나 PR에 재현 절차나 비밀 값을 올리지 않는다. 저장소 관리자([CODEOWNERS](.github/CODEOWNERS))에게 비공개로 알린다. 본선이 끝났으므로 운영 자원은 정리 대상이다. 신고에는 영향받는 경로와 커밋을 함께 적는다.
