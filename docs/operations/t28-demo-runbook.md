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

결과 `checked_items_ready`는 확인한 항목만 준비됐다는 뜻이다. 확인하지 않은 항목(`not_checked`)은 통과로 보지 않는다. 출발지 DB 쓰기 주체, 복제 방향, 플랫폼 계획 상태는 이 도구 범위 밖이다. 컷오버 뒤 원래 상태로 돌리려면 FAILBACK을 끝까지 실행하고 T13 실행기 결과로 확인한다.

## 장면과 명령

| 시각 | 장면 | 실행 | 담당 |
| --- | --- | --- | --- |
| 0:00 | S1 사전 기록 · 3등급 목록 · 통제 비교표, 게이트 1차 BLOCK(열린 첨부 버킷) | 콘솔(T21)에서 현재 계획 조회. 게이트 판정(T19) | 상무 |
| 0:40 | S2 에이전트 제안 → 리허설 차단 기록 → 검증 → 승인 → 목적지 반영 → 재대조 PASS | T10 최초 제안(Job 경로) → T15 리허설 → 사람 승인 → T31 `bravo-apply run` → `open_after_apply.py` | 지환 · 규태 · 동훈 |
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
  --kubeconfig <target> --application-id <T31 id> --receipts <T31 receipts> --wait 600
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
승인 직전에는 현재 T13 관찰·서명을 확인하고, 컷오버 승인 뒤에는 실제 실행 요청도
보내야 한다. 이 실행기는 승인을 만들거나 T13 자료를 추정하지 않는다.

회차의 `real/local/mock` 프로필과 시나리오는 처음 기록한 값으로 고정한다.
합성 회차를 `real`로 다시 열거나, 복구 회차를 정방향 회차로 바꿔 완료 처리할 수 없다.
기존 회차는 시나리오가 없으면 `recovery`로 해석한다. 헤더에는 등록 API·Reader·리허설
호스트 이미지 고정값을 기록한다. 코드 시험 통과와 실제 공용 환경 인수는 별도다.

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
