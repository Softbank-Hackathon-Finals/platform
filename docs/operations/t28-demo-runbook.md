# T28 데모 각본 · 시작 상태 · 대체 장면

설계 문서 10장의 270초 각본을 실행 명령과 담당에 연결한다. 이 문서는 실행 절차다. 리허설 결과는 매 회차 `docs/evidence/t28-…`에 따로 남긴다.

## 시작 전: 목적지 시작 상태

```sh
infra/envs/target-workloads/demo_reset.py check --kubeconfig <target> \
  --object-endpoint <target SeaweedFS> --probe-key <첨부 객체 key>
```

| 항목 | 시작 상태 | `apply`가 되돌리는가 |
| --- | --- | --- |
| 데모 장애 | 기록 없음, pooler replicas = ready | 예 (`demo_fault.py clear`) |
| 목적지 앱 쓰기 | `write_switch.py status` 통과, `closed` | 예 (`write_switch.py close`) |
| 첨부 버킷 | `open-demo` (S1 BLOCK용) | 아니오. 운영자가 `object_exposure.py plan --state open-demo`대로 연다 |
| Edge 가중치 | 목적지 0% | 아니오. T17 담당 도구로 확인한다 |

보안 판정 Pod를 데모 전에 기동 확인한다. 이 명령은 등록·승인·실행을 하지 않는다.

```sh
python3 operations/security-program/run.py warm --context <platform> --budget-seconds 45
```

AI 제안은 데모에서 T10의 `initial-proposal` Job이 실제 모델을 호출한다. 사전 준비에서는 정확한
입력·모델·고정 이미지가 설치되어 있고 Job 이미지를 노드가 받을 수 있는지만 확인한다. 미리 만든
제안을 새 실행의 결과처럼 재사용하지 않는다. T15는 같은 proposal/source/image digest를 격리
네임스페이스와 검증 Job에 결속하고, 정리 확인 뒤에만 승인 대기로 넘긴다.

결과 `checked_items_ready`는 확인한 항목만 준비됐다는 뜻이다. 확인하지 않은 항목(`not_checked`)은 통과로 보지 않는다. 출발지 DB 쓰기 주체, 복제 방향, 플랫폼 계획 상태는 이 도구 범위 밖이다. 컷오버 뒤 원래 상태로 돌리려면 FAILBACK을 끝까지 실행하고 T13 실행기 결과로 확인한다.

## 장면과 명령

| 시각 | 장면 | 실행 | 담당 |
| --- | --- | --- | --- |
| 0:00 | S1 사전 기록 · 3등급 목록 · 통제 비교표, 게이트 1차 BLOCK(열린 첨부 버킷) | 콘솔(T21)에서 현재 계획 조회. 게이트 판정(T19) | 상무 |
| 0:40 | S2 에이전트 제안 → 리허설 차단 기록 → 검증 → 보안 Review → 승인 → 목적지 반영 → 재대조 PASS | T10 최초 제안(Job 경로) → T15 리허설 → `patch/preflight` → 사람 승인 → T31 `bravo-apply run` → `open_after_apply.py` | 지환 · 규태 · 동훈 |
| 1:30 | S3 부하를 건 채 가중치 10 → 50 → 100% | T18 부하(예열 50 rps · 30초 → 본 측정), T17 가중치 전환 | 태현 · Edge 담당 |
| 2:10 | S3 컷오버: 쓰기 정지 시간, 무결성 영수증, 채팅 기록 유지 | 업로드 중지 → T8 첨부 복사(판정 `SUCCEEDED`) → T13 컷오버 실행기 | 태현 |
| 3:00 | S4 목적지 장애 → 알림 → 데이터째 되돌리기 | `demo_fault.py inject` → WATCH 판정 확인 → **`demo_fault.py clear`** → 되돌리기 승인 → FAILBACK → 역방향 복제 도착 확인 | 지환 · 태현 |
| 3:50 | 마무리: 감사 기록, 비용, 잔존 의존성 지도 | T24 감사 조회, 비용 조회, T27 지도 | 지환 |

**S4 순서를 지킨다.** 컷오버 실행기는 FAILBACK 중에 pooler를 다시 설정한다. 장애를 걸어 둔 채 되돌리기를 승인하지 않는다. `demo_fault.py inject`는 pooler를 0으로 줄이기 전에 원래 replicas와 Deployment UID를 `--record` 파일에 남긴다. `clear`는 같은 UID에만 그 수를 되돌린다. 복제는 pooler를 거치지 않고 DB끼리 연결하므로 장애 중에도 이어진다.

## 실행기 (`scripts/happy_path.py`)

해피패스 한 회차의 운영자 쪽 단계를 설계 7.6·8.2 순서로 진행하고 기록한다. plan 상태 전이는 T10(ADR-0031 워크플로 호스트)이 맡는다. 실행기는 상태를 바꾸거나 승인하지 않는다.

```sh
python3 scripts/happy_path.py steps                      # 순서와 연결 상태
python3 scripts/happy_path.py run --plan-id <plan> --record <run.jsonl> --profile real \
  --scenario recovery \
  --plan-state-command <plan 상태를 {"state": ...}로 출력하는 읽기 전용 명령> \
  --platform-context <platform> --patch-preflight <patch-preflight.json> \
  --patch-postflight <patch-postflight.json> --migration-preflight <migration-preflight.json> \
  --kubeconfig <target> --application-id <T31 id> --receipts <T31 receipts> \
  --wait 30 --step-timeout 60 --demo-budget 270
python3 scripts/happy_path.py confirm --record <run.jsonl> --step s2-change-approval --by <이름>
python3 scripts/happy_path.py resolve --record <run.jsonl> --step <id> --status done|failed --by <이름> --note <근거>
```

### 정방향 AWS → 온프렘 이전만 인수할 때

```sh
python3 scripts/happy_path.py run --plan-id <plan> --record <새 회차.jsonl> \
  --profile real --scenario migration --platform-context <platform> --kubeconfig <target> \
  --application-id <T31 id> --receipts <T31 receipts> --initial-args --from <initial.json>
```

`migration`은 정방향 컷오버·WATCH와 역방향 복제 도착 확인까지 진행한다.
S4 장애 주입·되돌리기는 실행하지 않는다. 최종 실제 계획을 다시 읽어 `WATCH` 또는
`DONE`이어야 완료를 기록한다. `DONE_ROLLED_BACK`은 정방향 이전 완료로 인정하지 않는다.
기본값 `recovery`는 기존 S4 포함 각본을 유지한다.

두 시나리오 모두 부하 측정과 첨부 복사 확인을 SHIFT·컷오버 승인보다 먼저 요구한다.
컷오버 승인 앞에는 `s3-security-prepare`(`prepare-cutover`)가 `patch/postflight`·`migration/preflight`
등록과 원격 Security Review PASS를 만든다. 컷오버 authority가 이 두 등록을 요구하므로, 이 단계 없이는
컷오버 승인이 보안 HOLD로 멈춘다. 데모는 사람 승인 경로를 쓴다. 사전 위임 경로(T49)는 보안 조건을
확인하지 않으므로(#924) 이 각본에서는 쓰지 않는다.
승인 직전에는 현재 T13 관찰·서명을 확인하고, 컷오버 승인 뒤에는 실제 실행 요청도
보내야 한다. 이 실행기는 승인을 만들거나 T13 자료를 추정하지 않는다.

회차의 `real/local/mock` 프로필과 시나리오는 처음 기록한 값으로 고정한다.
합성 회차를 `real`로 다시 열거나, 복구 회차를 정방향 회차로 바꿔 완료 처리할 수 없다.
기존 회차는 시나리오가 없으면 `recovery`로 해석한다. 헤더에는 등록 API·Reader·리허설
호스트 이미지 고정값을 기록한다. 코드 시험 통과와 실제 공용 환경 인수는 별도다.

### 270초 하드 버짓

- 첫 `run`이 JSONL 헤더에 시작 시각과 `demo_budget_seconds=270`을 고정한다. 사람 승인 때문에
  `confirm` 후 다시 실행해도 같은 마감 시각을 사용한다.
- 각 외부 명령의 timeout은 남은 전체 시간보다 길 수 없다. Security Review 준비는 그 안에서
  다시 45초 상한을 사용한다.
- 270초가 끝나면 다음 효과를 시작하지 않고 `demo_budget_exceeded`로 실패한다. 실행 중 응답을
  잃으면 `demo_budget_exhausted_during_step` UNKNOWN으로 멈추며 자동 재실행하지 않는다.
- 이 데모 실행기에는 시간 제한 해제 옵션이 없다. `--demo-budget`은 1~270초만 허용한다.
- 버짓을 맞추려고 보안 검사, 사람 승인, T15 검증 또는 실패 대조군을 건너뛰지 않는다. 라이브
  입력이 시간 안에 끝나지 않으면 아래 대체 장면으로 전환하고 범위를 그대로 밝힌다.
- 버짓을 넘기면(단계 전이든, 모든 단계를 마친 뒤든) 실행기가 plan 상태를 읽기 전용으로 한 번
  읽어 `plan_state_at_budget`으로 기록에 남긴다. 읽지 못하면 `unconfirmed`이고, 추정하지 않는다.
- **버짓 실패 뒤에는 같은 plan으로 새 기록을 만들지 않는다.** 마감 시각은 첫 기록에 고정돼 있어서
  `resolve`를 해도 다음 단계가 다시 버짓 실패로 끝난다. 새 기록을 만들면 `s2-ai-start` 같은 명령이
  같은 plan에 다시 실행될 수 있다. 다시 시연하려면 새 plan으로 처음부터 한다.

### 선행 조건: 보안 검토가 켜져 있으면 S2 반영 승인이 claim에서 `approval_stale`이 된다(#939 리뷰 1)

승인 호스트 설정의 `security_review`가 `false`이면 이 문제는 생기지 않는다(#989). 그때는 콘솔 승인과 T31 claim이
모두 감싸지 않은 authority를 써서 승인 context가 같다. 아래는 `security_review`를 켰을 때(값이 `false`가 아닐 때)다.
회차를 시작하기 전에 승인 호스트의 이 값을 확인한다.

보안 검토가 켜져 있으면 이 각본은 S2를 넘지 못한다. 콘솔 승인 경로(`operator_ports.py`, `trusted_rehearsal.py`)는
변경 승인 authority를 `SecurityReviewAuthority`로 감싸 승인 context revision이 `security-governance.v1`
지문을 담는다. 반면 T31 claim 경로(`workflow_application.py`의 `ChangeApprovalService`)는 감싸지 않은
`access.change_authority(ChangeIntake.current)`로 현재 context를 만든다. 그래서 `change.py`의 승인 context
대조가 늘 달라 claim이 `approval_stale`로 거부되고, application ID가 생기지 않아 `s2-open-writes`에서 멈춘다.
보안 우회는 아니다(실패 쪽으로 닫힌다). 수정 방법은 T31·T10 담당과 정한다(#958 제안). 그 수정이 develop에 들어가기 전에는
보안 검토를 켠 실제 환경의 S2 이후를 이 각본으로 인수하지 않는다.

- **기록:** 회차마다 JSONL 하나를 남긴다. 첫 줄은 develop SHA, GitOps 이미지 pin, 프로필(`real`/`local`/`mock`), plan이다. 이어서 단계마다 결과를 한 줄씩 쓴다. 다시 실행하면 끝난 단계 다음부터 이어 간다.
- **사람 판단:** 변경 승인, 컷오버 승인, 알림 확인, 되돌리기 승인에서 멈춘다. 승인은 콘솔에서 하고, 했다는 사실을 `confirm`으로 기록한다.
- **멈춤:** 결과가 불명(3)이거나 실패한 단계는 다시 돌리지 않는다. 실제 상태를 읽은 뒤 `resolve`로 기록해야 이어 간다. `SAFE_HOLD`·`NEEDS_HUMAN`이면 실패로 멈춘다.
- **아직 연결하지 않은 단계(`[unwired]`):** `s3-load`(T18), `s3-attachment-copy`·`s3-reverse-replication`(T8), `wrap-map`(T27), 그리고 plan 상태 읽기 명령(T10 `GET /v1/plans/{plan_id}`). 각 담당의 실행 명령이 정해지면 `STEPS`에 넣는다. 연결 전에는 그 단계에서 멈춘다.

## 중간 통합 점검

| 점검 | 통과 기준 |
| --- | --- |
| 1차 S1~S2 | 같은 plan에서 게이트 BLOCK 기록, 실제 AI 제안 결과, T15 리허설 결과, 승인 대기가 콘솔에 보인다 |
| 2차 S3~S4 | 같은 실행에서 가중치 단계별 T18 창, 컷오버 영수증, 장애 주입 뒤 WATCH 판정, FAILBACK 결과와 역방향 복제 도착이 남는다 |

## 대체 장면 (라이브 실패 시)

라이브가 실패하면 아래 기존 기록을 보여 준다. **표시 문구는 원문의 결론을 그대로 쓴다.** 로컬 시험을 실측처럼 말하지 않는다.

| 장면 | 기록 | 원문이 밝힌 범위 |
| --- | --- | --- |
| S1 열린 버킷 | [t9-open-bucket-2026-11-06](../evidence/t9-open-bucket-2026-11-06/README.md) | 로컬 kind 클러스터. 데모 목적지가 아님 |
| S2 반영 | [t31-apply-entry-2026-11-06](../evidence/t31-apply-entry-2026-11-06/README.md), [t31-policy-apply-2026-11-06](../evidence/t31-policy-apply-2026-11-06/README.md) | 원문 확인 후 표시 |
| S3 가중치 | [t18-joint-rehearsal-2026-11-06](../evidence/t18-joint-rehearsal-2026-11-06/README.md) | 로컬 대역. 실제 Edge 요청 0건 |
| S3 무결성 | [t13-real-fingerprint-2026-11-06](../evidence/t13-real-fingerprint-2026-11-06/README.md) | 실제 출발지·목적지 DB에서 `MATCH` |
| S3 첨부 | [t8-attachment-copy-real-2026-11-06](../evidence/t8-attachment-copy-real-2026-11-06/README.md) | 실제 경로에서 `SUCCEEDED` |
| S4 역방향 복제 | [t8-reverse-resume-2026-11-06](../evidence/t8-reverse-resume-2026-11-06/README.md) | 컷오버는 lab이 흉내 냄 |
| 마무리 지도 | [t27-real-map-2026-11-06](../evidence/t27-real-map-2026-11-06/README.md) | 실제 `target-dev`. 차단 판정은 `NOT_RUN` |
| 마무리 감사 | [t24-audit-live-2026-11-06](../evidence/t24-audit-live-2026-11-06/README.md) | 원문 확인 후 표시 |

해피패스를 실제로 실행한 뒤에는 그 실행의 기록으로 이 표를 바꾼다.
