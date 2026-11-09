# 회고와 한계 · 본선 해피패스(2026-11-06 ~ 11-08)

본선 동안 실제 AWS EKS 출발지에서 온프렘 RKE2 목적지로 앱을 옮기는 해피패스를 돌렸다. 이 문서는 그 과정에서 드러난 문제와 현재 한계를 정리한다. 기준은 develop `379f2ddc`, 공개 PR·증빙(`docs/evidence/`)·코드다. 팀 채널 기록은 사실 확인용으로만 썼고, 발언은 인용하지 않는다.

## 한 줄 결론

**잘못된 통과는 한 번도 없었다. 그러나 정상 경로로 PASS에 닿을 수도 없었다.** 실환경에서 확인한 범위는 S1(수집·분석·첫 GATE 판정)까지다. GATE의 실제 결과는 의도한 BLOCK 또는 UNKNOWN 대기뿐이었다. 마지막 데모(11/8, 1분 37초, [#1048](https://github.com/Softbank-Hackathon-Finals/platform/pull/1048))는 REHEARSE·GATE·WATCH를 건너뛰는 데모 모드로 진행했다. 그 데모에서 실제로 실행된 것은 Cutover Executor의 9단계(DONE, committed)다.

## 실환경에서 확인한 것과 확인하지 못한 것

| 단계 | 실환경 상태 | 근거 |
|---|---|---|
| S1 수집·분석·첫 GATE | 확인. 목적지 IAM-01.1(열린 버킷) 때문에 의도대로 BLOCK, 데이터 판정 PASS | [T14 GATE 기록용 실행](evidence/t14-gate-record-2026-11-08/README.md) |
| S2 AI 제안 | 여러 번 시도. 스트림 판정과 입장 만료로 멈춤, 일부만 통과 | [#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588), [#789](https://github.com/Softbank-Hackathon-Finals/platform/pull/789) |
| S2 리허설·변경 승인 | REHEARSE에서 약 93분 정체 뒤 데모 모드로 대체 | [#989](https://github.com/Softbank-Hackathon-Finals/platform/pull/989), [#1008](https://github.com/Softbank-Hackathon-Finals/platform/pull/1008), [#1009](https://github.com/Softbank-Hackathon-Finals/platform/pull/1009), [#1027](https://github.com/Softbank-Hackathon-Finals/platform/pull/1027) |
| GATE PASS | **실환경 0회** | 아래 "게이트" |
| S3 SHIFT·컷오버 | 데모 모드에서만 실행(Executor 실제 동작, SLI는 PASS로 기록) | [#1029](https://github.com/Softbank-Hackathon-Finals/platform/pull/1029), [#1048](https://github.com/Softbank-Hackathon-Finals/platform/pull/1048), [#1051](https://github.com/Softbank-Hackathon-Finals/platform/pull/1051) |
| S4 되돌리기 | 실환경 0회. Executor 되돌리기는 모의 환경에서만 확인 | [#212](https://github.com/Softbank-Hackathon-Finals/platform/pull/212), [#222](https://github.com/Softbank-Hackathon-Finals/platform/pull/222) |
| Edge 부하 측정 | 측정이 성립하지 않음 | 아래 "Edge·부하" |

## 드러난 문제

### Edge·부하

- 11/6 실제 Edge에서 처음 돌린 읽기 부하(200 rps, 60초)는 오류율 66%, 시간 초과 7,550건, p95 17.3초였다. 같은 조건의 재측정은 정상이었고, 원인은 확정하지 못했다. 기능 해피패스를 먼저 하기로 하고 진단을 멈췄다. ([T17 증빙](evidence/t17-edge-connectivity-2026-11-06/README.md))
- 합동 측정 리허설은 Cloudflare 대신 nginx 대역을 쓰고 노트북에서 부하를 보냈다. 두 번 모두 "판정 입력으로 쓸 수 없음"이었다. ([T18 증빙](evidence/t18-joint-rehearsal-2026-11-06/README.md))
- 실제 Cloudflare LB를 10%로 바꾸자 SLI 감시가 시작되지 않았다. T36 스키마가 이미지에 빠져 `FileNotFoundError`가 났기 때문이다. 바로 0%로 되돌렸다([#802](https://github.com/Softbank-Hackathon-Finals/platform/pull/802)).
- keep-alive 연결로는 목적지 비율이 1~4%로 나와 분배를 잴 수 없었다. LB가 연결 단위로 pool을 고르는 것으로 보인다. nginx 대역에서는 이 차이가 드러나지 않았다([#810](https://github.com/Softbank-Hackathon-Finals/platform/pull/810)).
- SHIFT의 연속 UNKNOWN 규칙은 정상 트래픽에서도 91%가 스스로 0%로 되돌아가는 규칙이었다. 1,600개 조합을 전수 실행해서 찾았다([#566](https://github.com/Softbank-Hackathon-Finals/platform/pull/566)).

### Job

- S2 AI 제안 Job에서 구독 스트림의 reasoning 항목이 거부돼 NEEDS_HUMAN이 됐다([#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)). 4차 시도는 최종 출력이 비어 `initial_proposal_expired`로 끝났고, 모델 effort를 낮추는 설정으로 고정했다([#789](https://github.com/Softbank-Hackathon-Finals/platform/pull/789)).
- Job을 띄우기 위한 승인 정책 예외가 계속 늘었다. CEL 식이 실서버 적용에 실패한 적도 있다([#877](https://github.com/Softbank-Hackathon-Finals/platform/pull/877), [#878](https://github.com/Softbank-Hackathon-Finals/platform/pull/878), [#886](https://github.com/Softbank-Hackathon-Finals/platform/pull/886)).
- 실제 트래픽이 없는 데모에서 SHIFT 창을 만들려고 플랫폼 부하 Job과 완화 프로필 `demo-shift-floor`를 추가했다([#847](https://github.com/Softbank-Hackathon-Finals/platform/pull/847)).
- AI 제안은 개인 구독 계정의 인증에 의존했다. 재인증이 막히자 S2가 멈췄고, API 키 방식으로 바꿨다.

### 플랫폼 서버(AWS RKE2)

- 워크플로 호스트가 `ModuleNotFoundError: bravo_apply`로 반복 재시작했다. 이미지 레시피에 패키지가 빠졌고, 빌드 검사는 진입점 하나만 import했다. 진입점마다 import 폐포를 검사하는 방식으로 고쳤다([#565](https://github.com/Softbank-Hackathon-Finals/platform/pull/565)).
- Reader 설정 v1·v2와 Recreate 전략이 맞물려, 이미지만 먼저 바뀌면 Reader가 멈추는 구조였다([#546](https://github.com/Softbank-Hackathon-Finals/platform/pull/546)).
- 승인 호스트는 새 이미지가 `security_review` 설정을 필수로 요구하고, 옛 이미지는 그 키가 있으면 시작을 거부했다. **어떤 순서로 올려도 안전하지 않은 조합**이었다([#983](https://github.com/Softbank-Hackathon-Finals/platform/pull/983), 되돌림 [#988](https://github.com/Softbank-Hackathon-Finals/platform/pull/988)).
- 이 밖에 다음 문제가 있었다.
  - SOVA MCP 연결 누수로 503이 연쇄됐다([#1005](https://github.com/Softbank-Hackathon-Finals/platform/pull/1005)).
  - Postgres가 중첩 트랜잭션을 거부했다([#1051](https://github.com/Softbank-Hackathon-Finals/platform/pull/1051)).
  - VPCE 정책의 Action이 바뀐 신뢰 방식과 맞지 않아 broker STS가 403이었다([#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)).
- 리허설 호스트 이미지 고정 PR은 76건 열렸고, 그중 40건이 병합되지 않고 닫혔다.

### 목적지 서버(온프렘)

- 목적지는 ARM Mac 위에서 x86_64를 TCG 에뮬레이션으로 실행한 개발 VM이다. 기능 결과와 성능 근거를 구분해야 한다.
- 그 밖에 다음 문제가 있었다.
  - 복제 receiver timeout 오류가 났다.
  - arm64에서 Tailscale 이미지가 exec format error를 냈다([#813](https://github.com/Softbank-Hackathon-Finals/platform/pull/813)).
  - Tailscale egress가 443을 Pod의 동적 포트로 넘겨 NetworkPolicy에 막혔다([#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)).
  - 고정 tailnet 이름과 인증서 이름이 달라 TLS 검증이 실패했다([#872](https://github.com/Softbank-Hackathon-Finals/platform/pull/872)).

### 게이트: 통과하지 못한 다섯 겹

1. **평가 프로필.** `full:1`은 시험 20개를 요구했지만, 저장된 결과는 PASS 1개, UNKNOWN 19개였다([T28 증빙](evidence/t28-full-path-preflight-2026-11-07/README.md)). 시험 3개짜리 `demo-gate:1`로 바꾸려면 계획을 새로 만들고 T14를 다시 실행해야 했다. 다시 실행한 결과도 이미 묶인 맥락 때문에 GATE에 기록하지 못했다.
2. **서명 신선도.** gate-result는 평가 후 60초 안에 운영자 키로 서명돼야 했다. 그런데 키가 호스트 밖에 있어 재평가 때마다 `gate_record_conflict`로 멈췄다. 플랫폼 키 자동 서명([#828](https://github.com/Softbank-Hackathon-Finals/platform/pull/828))은 열린 채 남았다.
3. **SHIFT 재평가.** 준비 상태 지문에 30~60초마다 새로 수집되는 스냅숏 지문이 들어갔다. 그래서 "GATE 때와 같아야 통과"라는 조건은 구조적으로 만족할 수 없었다(#565에서 비교 범위를 좁혀 해소).
4. **WATCH 재시험.** 항상 UNKNOWN을 돌려주는 자리표시 함수 때문에 DONE에 닿을 수 없었다(#748에서 제거).
5. **S2 승인 claim.** 콘솔 승인과 claim이 서로 다른 authority 래퍼를 써서 `approval_stale`이 났다. 코드 수정은 합의 단계에서 멈췄다([#958](https://github.com/Softbank-Hackathon-Finals/platform/pull/958)).

## 원인

1. **빅뱅 통합.** 팀원 5명이 작업 49개를 계약 기준으로 병렬 개발했다. 실제 클러스터에서 단계를 조립한 것은 결선 마지막 36시간이었다. 11/7 새벽까지 실환경 계획은 PROPOSE를 넘은 적이 없었다. 다음 공백은 시험 안에서만 조립돼 있었다.
   - 승인 API가 배포되지 않았다.
   - Executor 명령 토큰이 없어 제출이 403이었다.
   - Edge 운영 모듈이 없었다.
   - WATCH 포트가 없었다.
   - FAILBACK이 연결되지 않았다.
2. **시험이 생산자를 대역으로 바꿔 끼웠다.** 출처 인증, 시계, LB, 부하 위치를 대역으로 시험했다. #565에 "로컬 재현은 출처 인증을 대체해서 이 부분을 드러내지 못했다"고 적혀 있다. 실제로 움직이는 시계를 쓴 시험은 실패한 뒤에야 들어갔고, 들어가자 8건이 실패했다. 테스트는 각 부품이 **잘못 통과하지 않음**을 보였다. **조립하면 통과할 수 있음**은 보이지 않았다.
3. **게이트를 안전성만 기준으로 설계했다.** 조건 하나하나는 타당했다. 그러나 다섯 조건을 동시에 만족하는 실제 경로가 없었고, 그걸 확인하는 시험도 없었다.
4. **이미지와 설정이 따로 움직였다.** 호스트 설정(`ports.json`, `operator.json`)은 Git 밖의 ConfigMap이었다. 이미지와 설정 사이에 N/N+1 호환이 없어서, 롤아웃할 때마다 재시작과 되돌리기가 반복됐다.
5. **마감 직전에 품질 장치를 끄는 방식.** 데모 모드는 건너뛴 단계의 증거를 진짜 PASS와 같은 타입으로 기록한다(`services/platform-events/src/bravo_platform_events/workflow.py:207-233`). 구별할 수 있는 건 `demo-` 태그와 0으로 채운 digest뿐이다. #1027과 #1029는 필수 검사 `platform`이 실패(하위 검사 취소)한 상태로 병합됐다. 팀이 T28 증빙에 직접 쓴 원칙("프로필을 줄여 이 계획의 전체 PASS로 만들지 않는다")과 달리, 실제 기준은 20개 → 3개 → 건너뜀으로 내려갔다.

## 상태기계로 다시 읽기

막힘은 거의 전부 **활성(liveness)** 문제였다. "결국 PASS·DONE에 닿는가"가 깨졌다. **안전성(safety)**, 즉 "PASS 없이 전환하지 않는다"는 끝까지 지켜졌다. 이 구분으로 보면 같은 버그 세 개가 같은 원리로 설명된다.

| 원리 | 이번 사례 |
|---|---|
| 서로 다른 과거가 같은 이름이면 상태를 쪼갠다 | UNKNOWN 하나에 "변경 뒤 첫 창 대기"와 "관측 장애"가 섞여 있었다. 앞의 것을 HOLD로 분리해 해결했다([#566](https://github.com/Softbank-Hackathon-Finals/platform/pull/566)) |
| 빨리 바뀌는 데이터 축과 제어 상태를 분리한다 | 스냅숏 지문을 GATE 기록의 정체성에 넣어 SHIFT가 통과할 수 없었다. 비교에서 빼고 기록에만 남겨 해결했다([#565](https://github.com/Softbank-Hackathon-Finals/platform/pull/565)) |
| 결과 값에 "모름"과 "건너뜀"을 따로 둔다 | 데모 모드는 건너뜀을 PASS로 기록했다 |
| 규칙은 효과가 일어나는 자원에서 강제한다 | Executor는 epoch 펜스는 직접 검사하지만, 승인 기록은 검증하지 않는다(`services/cutover-executor/src/bravo_cutover/api.py:70-85`). 데모의 컷오버 authority가 실제 게이트 없이도 Executor를 통과한 이유다 |

배포도 같은 틀로 볼 수 있다. 이미지 버전 × 설정 버전의 상태 공간에서 "둘 다 Ready"로 가는 안전한 전이가 없었다([#983](https://github.com/Softbank-Hackathon-Finals/platform/pull/983), [#988](https://github.com/Softbank-Hackathon-Finals/platform/pull/988)).

## 잘 된 것

- 실경로에서 잘못된 PASS는 한 건도 없었다. 판정할 수 없으면 실패 쪽으로 닫혔다.
- 컷오버 Executor는 실제로 끝까지 동작했다. 다음 구조가 코드에 들어 있다.
  - 원격 호출 전에 의도를 먼저 기록한다.
  - 원격 호출 직전마다 소유권을 다시 확인한다.
  - 결과를 성공·실패·확인 못 함 셋으로 구분한다.
  - 커밋 뒤 실패하면 SAFE_HOLD로 멈춘다.
- 증빙마다 "증명하지 않는 것"을 명시했다(T17, T18, T28).
- #566의 1,600개 조합 전수 실행과 #565의 import 폐포 검사는 땜질이 아니라 구조적인 수정이었다.
- 11/7 새벽에 세운 2차 계획은 옳았다. 공백을 먼저 메우고, 로컬 E2E를 한 번 돌리고, 롤아웃은 한 번만 하고, 실환경 E2E 도중에는 코드를 고치지 않는다는 계획이었다. 끝까지 지키지 못했을 뿐이다.

## 현재 한계

- 실환경에서 GATE PASS → SHIFT → 컷오버 → WATCH → DONE을 한 번도 잇지 못했다. 되돌리기도 실환경에서 실행하지 않았다.
- 데모 모드(`demo.skip`, `operator.json`의 `demo`)와 자동 위임(`delegation.auto_approver`)이 코드에 남아 있다. 설정 한 줄로 사람 게이트를 우회할 수 있다.
- Executor는 승인을 직접 검증하지 않는다. 리허설 호스트 Pod 하나에 컷오버 명령 토큰과 Edge 토큰이 함께 있다(점검 문서에 해피패스 한정 위험으로 기록돼 있다).
- 호스트 설정이 Git 밖에 있어, 배포된 동작을 저장소만 보고는 확정할 수 없다.
- Edge의 실제 가중치 분배와 부하 p95는 측정되지 않았다. 목적지 성능 수치는 에뮬레이션 VM 기준이다.
- 열린 후속 작업: [#828](https://github.com/Softbank-Hackathon-Finals/platform/pull/828)(GATE 자동 서명), [#958](https://github.com/Softbank-Hackathon-Finals/platform/pull/958)(S2 claim 래퍼 통일), [#872](https://github.com/Softbank-Hackathon-Finals/platform/pull/872)(실환경 인수 요청).

## 다음에 다르게 할 것

1. **첫날에 실클러스터에서 걷는 뼈대를 만든다.** 모든 단계를 가짜 구현으로라도 끝까지 한 번 통과시킨 뒤 하나씩 진짜로 바꾼다.
2. **게이트마다 통과 증인 시험을 둔다.** 실제 형태의 생산자와 움직이는 시계로 PASS에 닿는 궤적 하나를 CI에 고정한다. 추상 계획 상태기계는 작은 BFS나 TLA+로 두 가지를 같이 검사한다.
   - 안전성: PASS 없이 SHIFT로 가는 경로가 없어야 한다.
   - 도달 가능성: PASS로 가는 경로가 있어야 한다.
3. **건너뜀은 별도 판정 값으로 기록한다.** Executor는 승인 기록을 직접 검증한다.
4. **설정을 Git에 두고 expand/contract로 바꾼다.** 새 이미지는 옛 설정을 받아들이고, 옛 이미지는 모르는 키를 무시한다.
5. **Edge 실측을 가장 먼저 한다.** 요청마다 새 연결을 여는 방식으로 잰다.
