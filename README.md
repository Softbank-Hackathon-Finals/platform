# SOVA · 클라우드 출구전략 플랫폼

<p align="center"><img alt="SOVA — 止めない。こぼさない。味はそのまま。 稼働中のサービスを、クラウドの外へ。" src="services/console/desktop/assets/sova-hero.jpg" width="100%"></p>

> **S**overeign(SoftBank) **O**pen **V**endor-neutral **A**rchitecture · SoftBank Hackathon 2026 본선 Team Bravo

**가동 중인 서비스를 AWS에서 온프레미스로 멈추지 않고 옮기고, 옮긴 환경이 원본과 같은 수준으로 안전하고 정확한지 검증 게이트로 확인한 뒤에만 전환한다.**

금융 서비스는 규제 · 비용 · 계약 종료 때문에 클라우드를 떠날 수 있어야 하고, 그 출구전략을 실제로 시험해야 한다.
그러나 기존 이전 도구는 "옮기기"만 보장할 뿐, 옮긴 환경이 원본과 같은 보안 수준과 데이터 정확성을 갖췄는지는 보장하지 않는다.
이 플랫폼은 AI가 격리된 리허설 환경에서 차이를 메우는 **제안**만 하고, 판정과 전환은 결정적 코드와 사람 승인이 맡는다. 문제가 생기면 그사이 쓴 데이터까지 들고 되돌아간다.

이 레포는 **플랫폼 코드 · 인프라 · 설계 문서 · 작업 관리**를 담는다. 이전 대상 데모 앱은 [`demo-chat`](https://github.com/Softbank-Hackathon-Finals/demo-chat)에 있다.

## 보안이 먼저다

SOVA는 보안을 앞세운다. **옮긴 환경이 원본과 같은 보안 수준이라는 것을 같은 시험으로 보이기 전에는 전환하지 않는다.** AI는 격리된 곳에서 제안만 하고, 판정·실행은 LLM을 쓰지 않는 결정적 코드와 사람 승인이 맡는다. 계층별 설계와 구현 상태(실환경 / 시험 / 설계)는 [SECURITY.md](SECURITY.md)에 있다.

| 계층 | 핵심 | 상태 |
|---|---|---|
| 전략 | SoftBank **Patching as a Service**(2026-07-14 발표)에서 착안한 "진단 → AI 수정 제안 → 격리 검증 → 승인 → 재진단" 순환을 이전에 적용하고, 막 공개된 취약점(Day-0) 대응으로 확장 설계(Day-0는 발표문이 아닌 팀의 해석) | 설계·일부 시험 |
| 보안 동등성 게이트 | 출발지·목적지에 같은 통제 시험(통제 6개·하위 시험 20개)을 돌리고, 다르거나 확인할 수 없으면 BLOCK | 실환경 일부(EKS·RKE2에서 시험 3개, 의도한 BLOCK 확인) |
| AI 에이전트 격리 | 도구 허용 목록, 턴당 함수 호출 1개, 600초 Job, broker 격리, 샌드박스 VM(`network=none`), 후보 코드 무실행 빌드. Harbor 반복 평가는 계획 | 실환경 일부 |
| 리허설 격리 | 무작위 네임스페이스, PSA restricted, 기본 거부, SA 토큰 없음 | 실환경 |
| 전환 안전 | epoch 펜싱, 의도 선기록, 확인 못 하면 SAFE_HOLD, Edge 0·10·50·100% | Executor 실행은 실환경(데모 모드 1회), 거부·복구 동작은 시험 |
| 탐지·감사 | 탐지 결과 OCSF 정규화(GuardDuty 실수집), 감사 기록 S3 Object Lock COMPLIANCE | 실환경 일부 |

보안 전략, 원본 대조 게이트, 통제 시험, 탐지 통합, Security Review는 이상무 님이 설계·구현했다([Security Governance v1](https://github.com/Softbank-Hackathon-Finals/security), [보안 문서](docs/security/README.md)). 아직 지키지 못한 것은 [SECURITY.md의 알려진 한계](SECURITY.md#알려진-한계)에 함께 적었다.

## 현재 상태와 한계

본선(2026-11-06 ~ 11-08)에서 실제 AWS EKS → 온프렘 RKE2 해피패스를 돌렸다. **잘못된 통과는 한 번도 없었지만, 실환경에서 GATE PASS에 닿지 못했다.** 실환경 확인은 S1(수집·분석·첫 GATE 판정, 의도한 BLOCK)까지다. 최종 데모는 리허설·게이트·감시를 건너뛰는 데모 모드로 진행했고, 그때 실제로 실행된 것은 컷오버 Executor다. 되돌리기와 Edge 부하 실측은 실환경에서 성립하지 않았다. 원인(빅뱅 통합, 대역 시험, 도달 가능성 없는 게이트, 이미지·설정 비호환)과 다음 개선은 [회고와 한계](docs/retrospective.md)에 정리했다.

## 콘솔

| 콘솔 | 만든 사람 | 형태 |
|---|---|---|
| [SOVA Console (데스크톱)](services/console/desktop/README.md) | 류지환 | Electron 앱. SOVA MCP에 본인 AWS SSO로 연결하고, 안내자(에이전트)와 함께 상태를 읽고 등록 초안을 만든다. 사용 안내 포함 |
| [Sova 이전 운영 콘솔](services/console/README.md) | 이상무 | 플랫폼 호스트의 T10 API에 붙는 웹 운영 화면. 보안 통제 보고서·탐지·관측·복귀·감사를 근거별로 보여 준다 |

## 아키텍처

**간략 구성도.** 기능 묶음 9개와 그 사이의 주요 흐름만 보인다.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture-overview-dark.png">
  <img alt="플랫폼 간략 구성도" src="docs/assets/architecture-overview.png">
</picture>

운영팀이 이전을 시작하면 플랫폼이 수집 → 분석 → AI 제안 → 리허설 → **원본 대조 게이트** → **사람 승인** → 전환 → 감시 · 되돌리기 순서로 진행한다. 게이트를 통과하고 승인을 받아야만 전환이 시작된다. 원본은 [설계 문서](docs/design.html) 7.1.

**상세 구성도.** 구성요소 20개와 작업 번호, 명령 · 자료 · 배포 경로를 나눠 보인다.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture-detail-dark.png">
  <img alt="플랫폼 상세 구성도" src="docs/assets/architecture-detail.png">
</picture>

두 그림은 develop `e2e39e06` · 설계 문서 v0.8 기준의 논리 구성도다. 화살표는 이전 단계의 순서가 아니다. 워크로드 기능별 대체(Cognito → Keycloak, SQS · Lambda → ElasticMQ 등)와 VPC · 접속 · 자격 발급 상세는 생략했다. 배치(어느 계정 · VPC · 머신에 무엇이 뜨는지)는 설계 문서 7.2.

## 구성

| 구성 | 설명 | 성격 |
|---|---|---|
| 콘솔 · API | 환경 등록, 진행 표시, 승인, 결과 조회 | 결정적 |
| 상태 머신 | 단계와 전이 조건을 고정하고 상태를 저장한다. 조건 없이 다음 단계로 넘어가지 않는다 | 결정적 |
| 수집기 | 출발지 · 목적지 설정을 읽기 전용으로 수집 | 결정적 |
| 에이전트 워커 | 의존성 분류, 번역 · 코드 수정 제안서(JSON Schema), 실패 원인 추정, 승인용 요약. 실행 권한 없음 | AI |
| 리허설 러너 · 도구 계층 | 목적지에 격리 네임스페이스를 만들고 에이전트 명령을 대리 실행, 검증 Job으로 채점 후 삭제 | 결정적 (안에서 AI가 시도) |
| 통제 시험기 · 원본 대조 게이트 | 6개 보안 통제를 원본과 대상에 똑같이 시험하고, 답이 다르면 전환을 막는다. LLM 미사용 | 결정적 |
| Cutover Executor | 쓰기를 수 초만 멈추는 전환 구간 실행, 무결성 비교, 되돌리기. LLM 미사용 | 결정적 |
| Edge 제어 | Cloudflare LB 가중치로 트래픽을 10 → 50 → 100% 옮기고 즉시 되돌린다 | 결정적 |
| 감사 로그 | 단계 전이 · 판정 · 승인 · 에이전트 입출력 · 차단을 지울 수 없게 기록 (S3 Object Lock) | 결정적 |

## 레포 구조

```
platform/
├── infra/                      # 설계 문서 8.8 구조로 채운다
│   ├── modules/                # 기능 계약 + 환경별 구현 (network · cluster · node · database · secret · object_store · edge · ci_identity)
│   └── envs/                   # source-aws · platform-aws · target-onprem
├── docs/
│   ├── design.html             # 설계 문서 최신본 (브라우저로 연다)
│   ├── design-history/         # 대체된 설계 문서 버전 사본과 버전 이력
│   ├── tasks.md                # 단계별 작업 · 역할 분담 (이슈와 자동 동기화)
│   ├── adr/                    # 결정 기록 (0000-template.md)
│   └── assets/                 # README 그림
├── services/                   # 플랫폼 서비스 22개 (아래 문단)
├── scripts/
│   ├── sync_tasks.py           # tasks.md ↔ 이슈 · 보드 동기화
│   └── design-bump.sh          # 설계 문서 버전 올리기
├── .agents/skills/todo-task/   # 작업 진행 스킬 (.claude/skills/todo-task는 링크)
├── .github/workflows/          # ci · sync-tasks
├── AGENTS.md                   # 에이전트 작업 안내 (CLAUDE.md가 불러온다)
└── README.md
```

플랫폼 코드는 `services/`에 서비스마다 폴더와 README가 있다(콘솔 · MCP · 상태 머신 · 에이전트 워커 · 통제 시험기 · 게이트 · Executor · Edge 제어 · 감사 로그 · 관측 등 22개).

## demo-chat과의 관계

플랫폼은 `demo-chat`을 **고객의 레포**처럼 다룬다. 그래서 레포를 나눴다.

| 무엇을 | 어떻게 |
|---|---|
| 앱 분석 | 레포 URL · 경로로 등록하고, 코드와 실제 배포를 대조한다 (T16) |
| 수정 반영 | 리허설을 통과하고 승인된 diff만 앱 레포 브랜치에 커밋한다 (T31). 토큰은 읽기 · 브랜치 쓰기로 좁힌다 |
| 이미지 | GitHub Actions가 빌드해 GHCR에 올리고, 출발지와 목적지가 같은 digest를 받는다 (T7) |

앱이 지켜야 할 조건(멱등 키, 재접속, 쓰기 차단 스위치 등)은 설계 문서 8.10과 `demo-chat`의 `AGENTS.md`에 있다.

## 워크플로

| 워크플로 | 하는 일 |
|---|---|
| `ci.yml` | PR · main/develop push마다 필수 검사 3개. `secrets`(PR 제목 형식 · gitleaks), `infra`(actionlint · hadolint · Terraform · Trivy config), `platform`(`pyproject.toml` · `package.json` · `go.mod`를 찾아 언어별 lint · test, Trivy fs) |
| `sync-tasks.yml` | `tasks.md`가 머지되면 계획을 이슈에 반영하고, 이슈의 체크 · 담당이 바뀌면 보드를 옮기고 `tasks.md`를 봇 PR로 고친다 |

## 팀원 작업 방법

1. [프로젝트 보드](https://github.com/orgs/Softbank-Hackathon-Finals/projects/2)나 [`docs/tasks.md`](docs/tasks.md)에서 맡은 작업을 찾는다. 진행 상황의 기준은 보드(이슈)다.
2. `develop`에서 `feature/T번호-설명` 브랜치를 만들어 `develop`으로 PR을 올린다. 대상 브랜치의 현재 ruleset·필수 CI·리뷰 요건을 충족한 뒤 merge commit으로 합친다. 이 조건을 GitHub가 확인하도록 기본 auto-merge를 예약할 수 있다. `main`은 release/hotfix로 반영한다. [Gitflow 순서](docs/development.md)와 [리뷰 안내](docs/review-playbook.md)를 따른다.
3. 커밋 메시지와 PR 제목 앞에 `[T번호]`. 끝낸 할 일은 **이슈에서** 체크한다. 보드 이동과 tasks.md 갱신은 자동이다. 계획(완료 기준 · 할 일 · 새 작업)을 바꿀 때는 `tasks.md`를 고쳐 PR로 올린다.
4. Claude Code에서는 `/todo-task`, Codex에서는 `$todo-task`로 이 과정을 스킬로 진행할 수 있다.

레포 세팅 · CI · 작업 흐름 · 이슈 봇을 처음 보는 사람을 위한 자세한 설명은 노션 [WorkGuide](https://app.notion.com/p/40c8bee9ada4802fb712d2df18eeb6e0)(팀원 전용)에 있다.

## 문서

- [계약 목차](docs/contracts/README.md): 보안·관측·무결성·승인 계약의 원본과 생산·소비 연결
- [설계 문서](docs/design.html): 브라우저로 열어 본다 (그림은 SVG로 들어 있다). 원본은 이 파일이다
- [설계 문서 버전 이력](docs/design-history/README.md): 버전을 올리는 기준과 이전 버전 사본
- [해야 할 일](docs/tasks.md): 단계별 작업과 역할 분담
- [결정 기록과 실측 근거](docs/adr/README.md): 설계별 ADR과 기존 측정 자료의 위치. 새 결정은 [`0000-template.md`](docs/adr/0000-template.md)로 쓴다
- [보안 설계 고려사항](docs/security/README.md): 스켈레톤 너머로 고려한 보안 설계 (추후 반영 후보, 원본은 노션)
- [에이전트 작업 안내](AGENTS.md): 작업 규칙, 설계 원칙, 보안 규칙, 검증 명령
- [보안](SECURITY.md): 계층별 보안 설계와 구현 상태, 취약점 신고
- [회고와 한계](docs/retrospective.md): 본선 해피패스에서 드러난 문제와 남은 한계

## 작업 현황

[Team Bravo Finals](https://github.com/orgs/Softbank-Hackathon-Finals/projects/2) 보드와 이 레포의 이슈를 본다.

칸반 조회·시작·완료 체크는 [프로젝트 보드 안내](docs/project-board.md)와 루트 Makefile을 사용한다.

본인 PR의 리뷰 도착은 `python3 scripts/review_watch.py`로 한 번 확인한다. `--watch`는 60초 이상 간격의 foreground 조회, `--notify`는 선택적 macOS 알림이다. `--install-git-alias`로 이 레포에 `git review-watch`를 설정할 수 있다. 첫 조회는 기존 리뷰 baseline을 만들며, 알림이 승인·병합 자격을 판정하지는 않는다. 사용법과 private cursor는 [리뷰 안내](docs/review-playbook.md#5-git-cli에서-리뷰-도착-확인), 실제 검토 절차는 [PR 리뷰 스킬](.agents/skills/pr-review/SKILL.md)을 따른다.
