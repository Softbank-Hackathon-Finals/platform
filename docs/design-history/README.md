# 설계 문서 버전 이력

설계 문서의 원본은 이 레포의 [`docs/design.html`](../design.html)이다(노션 페이지는 이 파일의 링크와 요약만 둔다). 항상 최신본이 이 경로에 있고, 대체된 버전의 사본은 이 폴더에 둔다. 사본은 고치지 않는다.

## 버전을 올리는 기준

| 변경 | 예 | 버전 |
|---|---|---|
| 작은 수정 | 오타, 실측값 갱신, 표현 다듬기 | 그대로. `design.html`을 고쳐 PR (git 이력으로 남는다) |
| 의미 있는 변경 | 아키텍처 · 시나리오 · 범위 · 요구사항 변경, 작업 계획 재편 | 올린다. `scripts/design-bump.sh <새 버전>`으로 시작한다 |

의미 있는 변경에는 결정이 따르므로 ADR(`docs/adr/`)을 함께 쓰고 아래 표에 연결한다. 작업 계획(11장)이 바뀌면 `docs/tasks.md`도 같이 고친다(작업 계획의 기준은 `tasks.md`).

## 버전 올리는 순서

1. `develop`에서 분리한 `feature/T번호-설명` 브랜치에서 실행한다. `git fetch origin develop`로 기준을 갱신하고 `git rev-parse origin/develop`의 커밋과 문서를 확인한 뒤 `scripts/design-bump.sh v0.4 --base <확인한 커밋>`을 실행한다. `--base`를 생략하면 로컬 `origin/develop`을 사용하며, 도구가 네트워크 조회를 하거나 기준이 최신이라고 보증하지 않는다.
2. 도구는 기준 커밋의 문서를 바이트 그대로 보관하고 작업 중 문서의 머리말·꼬리말과 이력 표를 갱신한다. 기존 본문 편집은 보존한다. 기준과 작업 문서 버전 불일치, 잘못된 표/꼬리말, 중복 사본, 이전·동일 버전은 쓰기 전에 거부한다. `main`·`develop`·분리된 HEAD에서는 실행하지 않는다.
3. `design.html` 본문과 아래 표의 "무엇이 바뀌었나" · "관련 ADR"을 채운다.
4. 변경을 검토·커밋하고 `develop`으로 PR을 올린다. 도구는 브랜치 전환, staging, 태그 생성·push를 하지 않는다. 차이는 PR에서 기준 커밋과 비교한다. 릴리스 게시가 필요하면 병합 후 별도 릴리스 절차로 수행한다.

## 놓치지 않게 하는 장치

- **에이전트**: `todo-task` 스킬이 설계 문서를 고치기 전에 변경 크기를 판단하고, 의미 있는 변경이면 사용자에게 버전 번호를 확인받아 이 스크립트를 먼저 실행한다.
- **CI 경고**: PR이 `design.html`을 바꿨는데 버전 표기가 그대로면 `design-doc` 검사가 PR 화면(바뀐 파일의 버전 줄)에 경고를 남긴다. 필수 검사가 아니라 머지를 막지 않는다. 작은 수정이면 무시한다.

## 버전 목록

| 버전 | 기간 | 파일 | 무엇이 바뀌었나 | 관련 ADR |
|---|---|---|---|---|
| **v0.8 (현재 초안)** | 2026-11-06 ~ | [`docs/design.html`](../design.html) | 8.3·아키텍처 그림·T11의 OpenAI 실행을 Agents SDK로 전환. SIWC 인증·도구 정책은 유지하고 SDK 실행 추적을 플랫폼 OTEL에 연결 | [ADR-0007: 2026-11-06](../adr/0007-model-provider-and-s2-ci.md#2026-11-06-openai-agents-sdk-및-otel-실행-추적) |
| v0.7 | 2026-11-05 ~ 2026-11-06 | [`design-v0.7.html`](design-v0.7.html) | 11장 작업 카드를 `tasks.md`에 맞춤. T12 · T20 완료 기준을 k3d · 모의 환경으로 한정하고, 실제 환경의 첫 컷오버 · 되돌리기를 T25로 옮김. T25에 부하 조건(5.3), 회차별 실패 · 유실 · 중복 건수(NFR-2), 장애별 기대 결과(8.6), 시간 초과 자동 재개 금지 확인(NFR-3), 실제 Tailscale 단절 중 되돌리기 거절 확인, 증빙 위치, 통합 시점 T8 · T30을 추가 | [ADR-0011](../adr/0011-cutover-executor-execution-and-failure.md) · [ADR-0016](../adr/0016-failback-replication-path-check.md) · [ADR-0022](../adr/0022-failback-state-machine-flow.md) |
| v0.6 | 2026-11-05 ~ 2026-11-05 | [`design-v0.6.html`](design-v0.6.html) | T7 저장소 접근을 분석 reader 수신 증빙으로 정합화. T31은 리허설 검증 digest 승격이며 승인 뒤 Git 커밋·재빌드와 writer 주입을 요구하지 않음. T31 정책·게이트 완료 기준은 유지 | [ADR-0021 §6](../adr/0021-approved-change-promotion.md#6-t7-저장소-접근-범위-정합화-2026-11-05) |
| v0.5 | 2026-11-04 ~ 2026-11-05 | [`design-v0.5.html`](design-v0.5.html) | 6장 S4 되돌리기 예외 흐름과 8.6 컷오버 순서 1단계: 쓰기를 막기 전 시험 barrier 왕복으로 복제 경로를 확인하고(단절이면 변경 없이 거절), 쓰기를 막은 뒤 출발지가 따라잡지 못하면 SAFE_HOLD 대신 목적지를 원상 복구(ABORT_PRE_WRITE). 확인 불가는 기존대로 SAFE_HOLD | [ADR-0016](../adr/0016-failback-replication-path-check.md) |
| v0.4 | 2026-11-03 ~ 2026-11-04 | [`design-v0.4.html`](design-v0.4.html) | 8.3 모델 제공자와 S2 CI 실패 분석: 관리자 구독(SIWC) 기반 OpenAI Responses 워커, 선택형 Claude 어댑터, 제안·검증·반영 경계 | [ADR-0007](../adr/0007-model-provider-and-s2-ci.md) |
| v0.3 | 2026-11-03 | [`design-v0.3.html`](design-v0.3.html) | 8.9 계획 조회 제안: 상태 · 이벤트 원자 저장, snapshot watermark, SSE와 동일한 계획 접근 정책 | [ADR-0008](../adr/0008-plan-state-event-transaction.md) |
| v0.2 | 2026-11-03 | [`design-v0.2.html`](design-v0.2.html) | 8.4 리허설 네임스페이스 격리 규칙: 송신 허용 범위에 시험용 SeaweedFS · OpenBao(지정 Pod · 포트)를 넣고, 서버 안 권한(T9)을 격리의 일부로 명시. 7.5 네트워크 경로 규칙 표에 허용 줄 추가 | [ADR-0002](../adr/0002-rehearsal-egress-allowlist.md) |
| v0.1 | 2026-11-02 ~ 2026-11-03 | [`design-v0.1.html`](design-v0.1.html) · [태그](https://github.com/Softbank-Hackathon-Finals/platform/tree/design-v0.1) | 첫 버전 (11/2 회의 반영: 역할별 담당자, 이전 대상 추천 P1) |  |

### v0.3 보관 기준

개발 정본인 `develop`의 `c7822e5fb1f6b4adea4392914ff4010e5a258dcd`에서 v0.2를 바이트 그대로 보관했다. 당시 bump 스크립트는 `origin/main`을 읽고 원격 태그를 자동 게시했으므로 해당 변경에는 실행하지 않았다. 이후 도구는 위의 로컬 준비 절차로 수정했다. 기존 사본과 그 보관 기준은 그대로 유지한다.

### v0.4 보관 기준

T10의 v0.3이 먼저 병합되어, 개발 정본 `develop`의 `2c43d59914efe9a59133ce285923faa185dcbfe5`에서 v0.3을 바이트 그대로 보관했다. 이미 보관된 v0.2는 변경하지 않았다. T11의 모델 제공자 제안은 v0.4로 구분하고 T10 계획 조회 계약은 유지한다. 보관 당시 bump 스크립트는 `origin/main`과 원격 태그 게시를 전제로 하므로 사본을 직접 보관했다. 이후 도구는 위의 로컬 준비 절차로 수정됐으며, 이 이력과 기존 보관본은 당시 기준을 유지한다.

### v0.5 보관 기준

개발 정본 `develop`의 `e20e338085085806c04002a8b7e6af8667996a19`에서 v0.4를 `scripts/design-bump.sh v0.5 --base <커밋>`으로 바이트 그대로 보관했다.

### v0.6 보관 기준

개발 정본 `develop`의 `59e93e1d92f449d4701c3aae31b7d16d4bc14a22`에서 v0.5를 `scripts/design-bump.sh v0.6 --base <커밋>`으로 바이트 그대로 보관했다. 사용자의 직접 해결·공식 기준 정합화 위임 범위에서 다음 버전을 선택했다.

### v0.7 보관 기준

개발 정본 `develop`의 `3f310441f31d993f3cfe072f7618ce35adfcc2fa`에서 v0.6을 `scripts/design-bump.sh v0.7 --base <커밋>`으로 바이트 그대로 보관했다. 새 결정은 없고, ADR-0011 · 0016 · 0022가 T25로 넘긴 실제 환경 확인을 작업 카드에 반영했다.
