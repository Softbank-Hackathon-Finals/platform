# 회고와 한계: 본선 해피패스(2026-11-06 ~ 11-08)

본선 기간에 팀은 실제 AWS EKS 출발지에서 온프렘 RKE2 목적지로 앱을 옮기는 해피패스를 실행했습니다. 이 문서는 그 과정에서 드러난 문제와 현재 남아 있는 한계를 정리합니다. 기준 자료는 develop `379f2ddc`의 코드, 공개 PR, 증빙(`docs/evidence/`)입니다. 팀 채널 기록은 사실을 확인하는 데에만 사용했으며, 팀원의 발언은 인용하지 않았습니다.

## 결론

**잘못 통과한 판정은 한 번도 없었습니다. 그러나 정상 경로로는 PASS에 도달할 수도 없었습니다.** 실환경에서 확인한 범위는 S1(수집, 분석, 첫 GATE 판정)까지이며, 실제 GATE 결과는 의도한 BLOCK이거나 UNKNOWN 상태의 대기뿐이었습니다. 마지막 데모(11/8, 1분 37초, [#1048](https://github.com/Softbank-Hackathon-Finals/platform/pull/1048))는 REHEARSE, GATE, WATCH를 건너뛰는 데모 모드로 진행했습니다. 그 데모에서 실제로 실행된 부분은 Cutover Executor의 9단계(DONE, committed)입니다.

## 실환경에서 확인한 범위

| 단계 | 실환경 상태 | 근거 |
|---|---|---|
| S1 수집, 분석, 첫 GATE | 확인했습니다. 목적지의 IAM-01.1(열린 버킷) 때문에 의도대로 BLOCK했고, 데이터 판정은 PASS였습니다 | [T14 GATE 기록용 실행](evidence/t14-gate-record-2026-11-08/README.md) |
| S2 AI 제안 | 여러 번 시도했지만 스트림 판정과 입장 허가 만료로 멈췄고, 일부 시도만 통과했습니다 | [#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588), [#789](https://github.com/Softbank-Hackathon-Finals/platform/pull/789) |
| S2 리허설과 변경 승인 | REHEARSE 단계에서 약 93분 동안 정체된 뒤 데모 모드로 대체했습니다 | [#989](https://github.com/Softbank-Hackathon-Finals/platform/pull/989), [#1008](https://github.com/Softbank-Hackathon-Finals/platform/pull/1008), [#1009](https://github.com/Softbank-Hackathon-Finals/platform/pull/1009), [#1027](https://github.com/Softbank-Hackathon-Finals/platform/pull/1027) |
| GATE PASS | **실환경에서 0회**였습니다 | 아래 "게이트" 절 |
| S3 SHIFT와 컷오버 | 데모 모드에서만 실행했습니다. Executor는 실제로 동작했지만 SLI 판정은 PASS로 기록되었습니다 | [#1029](https://github.com/Softbank-Hackathon-Finals/platform/pull/1029), [#1048](https://github.com/Softbank-Hackathon-Finals/platform/pull/1048), [#1051](https://github.com/Softbank-Hackathon-Finals/platform/pull/1051) |
| S4 되돌리기 | 실환경에서는 실행하지 않았습니다. Executor의 되돌리기는 모의 환경에서만 확인했습니다 | [#212](https://github.com/Softbank-Hackathon-Finals/platform/pull/212), [#222](https://github.com/Softbank-Hackathon-Finals/platform/pull/222) |
| Edge 부하 측정 | 측정 방식의 한계로 결과가 성립하지 않았습니다 | 아래 "Edge와 부하" 절 |

## 드러난 문제

### Edge와 부하

- 11/6에 실제 Edge에서 처음 실행한 읽기 부하(200 rps, 60초)의 결과는 오류율 66%, 시간 초과 7,550건, p95 17.3초였습니다. 같은 조건으로 다시 측정했을 때는 정상이었으며, 원인은 확정하지 못했습니다. 팀은 기능 해피패스를 먼저 진행하기로 하고 진단을 중단했습니다([T17 증빙](evidence/t17-edge-connectivity-2026-11-06/README.md)).
- 합동 측정 리허설에서는 Cloudflare 대신 nginx 대역을 사용했고, 부하도 노트북에서 보냈습니다. 두 번의 실행 모두 "판정 입력으로 사용할 수 없음"으로 판정되었습니다([T18 증빙](evidence/t18-joint-rehearsal-2026-11-06/README.md)).
- 실제 Cloudflare LB의 가중치를 10%로 바꾸자 SLI 감시가 시작되지 않았습니다. T36 스키마 파일이 이미지에 포함되지 않아 `FileNotFoundError`가 발생했기 때문이며, 팀은 즉시 가중치를 0%로 되돌렸습니다([#802](https://github.com/Softbank-Hackathon-Finals/platform/pull/802)).
- keep-alive 연결을 유지한 부하에서는 목적지로 간 요청 비율이 1~4%에 그쳐 가중치 분배를 측정할 수 없었습니다. LB가 연결 단위로 pool을 선택하는 것으로 보이며, nginx 대역에서는 이 차이가 드러나지 않았습니다([#810](https://github.com/Softbank-Hackathon-Finals/platform/pull/810)).
- SHIFT 단계의 연속 UNKNOWN 규칙을 적용하면 정상 트래픽에서도 91%의 경우에 가중치가 스스로 0%로 되돌아갔습니다. 이 문제는 1,600개 조합을 전수 실행해 찾아냈습니다([#566](https://github.com/Softbank-Hackathon-Finals/platform/pull/566)).

### Job

- S2 AI 제안 Job에서는 구독 스트림의 reasoning 항목이 거부되어 결과가 NEEDS_HUMAN이 되었습니다([#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)). 4차 시도에서는 최종 출력이 비어 `initial_proposal_expired`로 끝났고, 팀은 모델의 effort를 낮추는 설정으로 고정했습니다([#789](https://github.com/Softbank-Hackathon-Finals/platform/pull/789)).
- Job을 실행하기 위한 승인 정책 예외가 계속 늘어났고, CEL 식을 실서버에 적용하지 못한 경우도 있었습니다([#877](https://github.com/Softbank-Hackathon-Finals/platform/pull/877), [#878](https://github.com/Softbank-Hackathon-Finals/platform/pull/878), [#886](https://github.com/Softbank-Hackathon-Finals/platform/pull/886)).
- 실제 트래픽이 없는 데모에서 SHIFT 판정 창을 만들기 위해 플랫폼 부하 Job과 완화된 판정 프로필 `demo-shift-floor`를 추가했습니다([#847](https://github.com/Softbank-Hackathon-Finals/platform/pull/847)).
- AI 제안은 개인 구독 계정의 인증에 의존했습니다. 재인증을 할 수 없게 되자 S2가 멈췄고, 팀은 API 키 방식으로 전환했습니다.

### 플랫폼 서버(AWS RKE2)

- 워크플로 호스트가 `ModuleNotFoundError: bravo_apply`로 재시작을 반복했습니다. 이미지 레시피에서 패키지가 빠져 있었고, 빌드 검사는 진입점 하나만 import했기 때문입니다. 팀은 진입점마다 import 폐포를 검사하도록 고쳤습니다([#565](https://github.com/Softbank-Hackathon-Finals/platform/pull/565)).
- Reader 설정 v1·v2와 Recreate 배포 전략이 맞물려, 이미지만 먼저 바뀌면 Reader가 멈추는 구조였습니다([#546](https://github.com/Softbank-Hackathon-Finals/platform/pull/546)).
- 승인 호스트의 새 이미지는 `security_review` 설정을 필수로 요구했고, 이전 이미지는 그 설정 키가 있으면 시작을 거부했습니다. 따라서 **어떤 순서로 배포해도 안전하지 않은 조합**이었습니다([#983](https://github.com/Softbank-Hackathon-Finals/platform/pull/983), 되돌림 [#988](https://github.com/Softbank-Hackathon-Finals/platform/pull/988)).
- 그 밖에도 다음과 같은 문제가 있었습니다.
  - SOVA MCP의 연결 누수 때문에 503 응답이 연쇄적으로 발생했습니다([#1005](https://github.com/Softbank-Hackathon-Finals/platform/pull/1005)).
  - 실환경 Postgres가 중첩 트랜잭션을 거부했습니다([#1051](https://github.com/Softbank-Hackathon-Finals/platform/pull/1051)).
  - VPC 엔드포인트 정책의 Action이 바뀐 신뢰 방식과 맞지 않아 broker의 STS 요청이 403으로 거부되었습니다([#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)).
- 리허설 호스트 이미지를 고정하는 PR은 76건이 열렸고, 그중 40건은 병합되지 않은 채 닫혔습니다.

### 목적지 서버(온프렘)

- 목적지는 ARM Mac에서 x86_64를 TCG 에뮬레이션으로 실행한 개발 VM입니다. 따라서 기능 결과와 성능 근거는 구분해서 읽어야 합니다.
- 그 밖에도 다음과 같은 문제가 있었습니다.
  - 복제 경로에서 receiver timeout 오류가 발생했습니다.
  - arm64 노드에서 Tailscale 이미지가 exec format error를 냈습니다([#813](https://github.com/Softbank-Hackathon-Finals/platform/pull/813)).
  - Tailscale egress가 443 포트를 Pod의 동적 포트로 넘기는 바람에 NetworkPolicy에 막혔습니다([#588](https://github.com/Softbank-Hackathon-Finals/platform/pull/588)).
  - 고정 tailnet 이름과 인증서 이름이 달라 TLS 검증이 실패했습니다([#872](https://github.com/Softbank-Hackathon-Finals/platform/pull/872)).

### 게이트: 통과를 막은 다섯 가지 조건

1. **평가 프로필.** `full:1` 프로필은 시험 20개를 요구했지만, 저장된 결과는 PASS 1개와 UNKNOWN 19개였습니다([T28 증빙](evidence/t28-full-path-preflight-2026-11-07/README.md)). 시험 3개로 구성된 `demo-gate:1`로 바꾸려면 계획을 새로 만들고 T14를 다시 실행해야 했으며, 다시 실행한 결과도 이미 결속된 맥락 때문에 GATE에 기록하지 못했습니다.
2. **서명의 신선도.** gate-result는 평가 후 60초 안에 운영자 키로 서명되어야 했습니다. 그러나 키가 호스트 밖에 있었기 때문에 재평가할 때마다 `gate_record_conflict`로 멈췄습니다. 플랫폼 키로 자동 서명하는 PR([#828](https://github.com/Softbank-Hackathon-Finals/platform/pull/828))은 아직 열려 있습니다.
3. **SHIFT 재평가.** 준비 상태 지문에 30~60초마다 새로 수집되는 스냅숏 지문이 포함되어 있었습니다. 그래서 "GATE 시점과 같아야 통과한다"는 조건은 구조적으로 만족할 수 없었습니다. 이 문제는 비교 범위를 좁혀 해결했습니다([#565](https://github.com/Softbank-Hackathon-Finals/platform/pull/565)).
4. **WATCH 재시험.** 항상 UNKNOWN을 반환하는 자리표시 함수 때문에 WATCH가 DONE에 도달할 수 없었습니다. 이 함수는 [#748](https://github.com/Softbank-Hackathon-Finals/platform/pull/748)에서 제거했습니다.
5. **S2 승인 claim.** 콘솔의 승인 경로와 claim 경로가 서로 다른 authority 래퍼를 사용해 `approval_stale`이 발생했습니다. 코드 수정은 담당자 간 합의 단계에서 멈췄습니다([#958](https://github.com/Softbank-Hackathon-Finals/platform/pull/958)).

## 원인

1. **빅뱅 통합.** 팀원 5명이 49개 작업을 계약 기준으로 병렬 개발했고, 실제 클러스터에서 단계를 조립한 시점은 결선 마지막 36시간이었습니다. 11/7 새벽까지 실환경 계획은 PROPOSE 단계를 넘은 적이 없었습니다. 그때까지 다음 연결은 시험 안에서만 조립되어 있었습니다.
   - 승인 API가 배포되지 않았습니다.
   - Executor에 명령 토큰이 없어 제출이 403으로 거부되었습니다.
   - Edge 운영 모듈이 저장소에 없었습니다.
   - 워크플로 호스트에 WATCH 포트가 없었습니다.
   - FAILBACK 경로가 연결되지 않았습니다.
2. **시험이 실제 생산자를 대역으로 대체했습니다.** 출처 인증, 시계, LB, 부하 발생 위치를 모두 대역으로 시험했습니다. [#565](https://github.com/Softbank-Hackathon-Finals/platform/pull/565)에도 "로컬 재현은 출처 인증을 대체해서 이 부분을 드러내지 못했다"고 기록되어 있습니다. 실제로 흐르는 시계를 사용하는 시험은 실패를 겪은 뒤에야 추가되었고, 추가하자마자 8건이 실패했습니다. 테스트는 각 부품이 **잘못 통과하지 않는다**는 사실은 보여 주었지만, **조립했을 때 통과할 수 있다**는 사실은 보여 주지 못했습니다.
3. **게이트를 안전성 기준으로만 설계했습니다.** 각 조건은 타당했지만, 다섯 조건을 동시에 만족하는 실제 경로가 없었고 이를 확인하는 시험도 없었습니다.
4. **이미지와 설정이 따로 변경되었습니다.** 호스트 설정(`ports.json`, `operator.json`)은 Git 밖의 ConfigMap에 있었고, 이미지와 설정 사이에 N/N+1 호환성이 없었습니다. 그래서 롤아웃할 때마다 재시작과 되돌림이 반복되었습니다.
5. **마감 직전에 품질 장치를 끄는 방식을 택했습니다.** 데모 모드는 건너뛴 단계의 증거를 실제 PASS와 같은 타입으로 기록합니다(`services/platform-events/src/bravo_platform_events/workflow.py:207-233`). 두 증거를 구별할 수 있는 단서는 `demo-` 태그와 0으로 채운 digest뿐입니다. [#1027](https://github.com/Softbank-Hackathon-Finals/platform/pull/1027)과 [#1029](https://github.com/Softbank-Hackathon-Finals/platform/pull/1029)는 필수 검사 `platform`이 실패한 상태(하위 검사 취소)에서 병합되었습니다. 팀은 T28 증빙에 "프로필을 줄여 이 계획의 전체 PASS로 만들지 않는다"는 원칙을 적어 두었지만, 실제 판정 기준은 시험 20개에서 3개로 줄었다가 결국 건너뛰기로 바뀌었습니다.

## 상태기계 관점에서 다시 읽기

정체의 거의 모든 원인은 **활성(liveness)** 문제였습니다. 즉 "결국 PASS나 DONE에 도달하는가"라는 성질이 깨졌습니다. 반면 **안전성(safety)**, 즉 "PASS 없이 전환하지 않는다"는 성질은 끝까지 지켜졌습니다. 이 구분을 적용하면 서로 달라 보이던 버그를 같은 원리로 설명할 수 있습니다.

| 원리 | 이번 사례 |
|---|---|
| 서로 다른 과거를 같은 상태 이름으로 묶었다면 상태를 나눕니다 | UNKNOWN 하나에 "변경 뒤 첫 판정 창을 기다리는 상태"와 "관측 장애"가 섞여 있었습니다. 앞의 상태를 HOLD로 분리해 해결했습니다([#566](https://github.com/Softbank-Hackathon-Finals/platform/pull/566)) |
| 빠르게 바뀌는 데이터 축과 제어 상태를 분리합니다 | 스냅숏 지문을 GATE 기록의 정체성에 포함해 SHIFT가 통과할 수 없었습니다. 지문을 비교 대상에서 빼고 기록에만 남겨 해결했습니다([#565](https://github.com/Softbank-Hackathon-Finals/platform/pull/565)) |
| 결과 값에 "모름"과 "건너뜀"을 별도로 둡니다 | 데모 모드는 건너뛴 단계를 PASS로 기록했습니다 |
| 규칙은 효과가 일어나는 자원 쪽에서 강제합니다 | Executor는 epoch 펜스는 직접 검사하지만 승인 기록은 검증하지 않습니다(`services/cutover-executor/src/bravo_cutover/api.py:70-85`). 데모의 컷오버 authority가 실제 게이트 없이도 Executor를 통과한 이유가 여기에 있습니다 |

배포 과정도 같은 관점으로 볼 수 있습니다. 이미지 버전과 설정 버전을 곱한 상태 공간에서, 두 구성 요소가 모두 Ready가 되는 상태로 가는 안전한 전이가 없었습니다([#983](https://github.com/Softbank-Hackathon-Finals/platform/pull/983), [#988](https://github.com/Softbank-Hackathon-Finals/platform/pull/988)).

## 잘 된 점

- 실제 경로에서 잘못된 PASS는 한 건도 없었습니다. 판정할 수 없는 상황에서는 항상 실패 쪽으로 닫혔습니다.
- 컷오버 Executor는 실제로 끝까지 동작했으며, 코드에는 다음 구조가 들어 있습니다.
  - 원격 호출 전에 의도를 먼저 기록합니다.
  - 원격 호출 직전마다 소유권을 다시 확인합니다.
  - 결과를 성공, 실패, 확인 불가 세 가지로 구분합니다.
  - 커밋 이후에 실패하면 SAFE_HOLD로 멈춥니다.
- 증빙 문서마다 "증명하지 않는 것"을 명시했습니다(T17, T18, T28).
- [#566](https://github.com/Softbank-Hackathon-Finals/platform/pull/566)의 1,600개 조합 전수 실행과 [#565](https://github.com/Softbank-Hackathon-Finals/platform/pull/565)의 import 폐포 검사는 임시방편이 아닌 구조적인 수정이었습니다.
- 11/7 새벽에 세운 2차 계획의 방향은 옳았습니다. 공백을 먼저 메우고, 로컬 E2E를 한 번 실행하고, 롤아웃은 한 번만 하고, 실환경 E2E 도중에는 코드를 고치지 않는다는 계획이었지만, 끝까지 지키지는 못했습니다.

## 현재 남아 있는 한계

- 실환경에서 GATE PASS, SHIFT, 컷오버, WATCH, DONE을 한 번도 연속으로 완료하지 못했고, 되돌리기도 실환경에서 실행하지 않았습니다.
- 데모 모드(`demo.skip`, `operator.json`의 `demo`)와 자동 위임(`delegation.auto_approver`) 설정이 코드에 남아 있어, 설정 한 줄로 사람의 승인 게이트를 우회할 수 있습니다.
- Executor는 승인을 직접 검증하지 않습니다. 또한 리허설 호스트 Pod 하나에 컷오버 명령 토큰과 Edge 토큰이 함께 있으며, 점검 문서에는 이를 해피패스 기간에 한정한 위험으로 기록했습니다.
- 호스트 설정이 Git 밖에 있으므로 저장소만 보고는 배포된 동작을 확정할 수 없습니다.
- Edge의 실제 가중치 분배와 부하 p95는 측정하지 못했고, 목적지 성능 수치는 에뮬레이션 VM을 기준으로 합니다.
- 열린 후속 작업으로는 [#828](https://github.com/Softbank-Hackathon-Finals/platform/pull/828)(GATE 자동 서명), [#958](https://github.com/Softbank-Hackathon-Finals/platform/pull/958)(S2 claim 래퍼 통일), [#872](https://github.com/Softbank-Hackathon-Finals/platform/pull/872)(실환경 인수 요청)가 있습니다.

## 다음에 다르게 할 일

1. **첫날에 실제 클러스터에서 최소 종단 경로(walking skeleton)를 만듭니다.** 모든 단계를 임시 구현으로라도 끝까지 한 번 통과시킨 뒤, 단계별로 실제 구현으로 교체합니다.
2. **게이트마다 PASS에 도달하는 시험을 둡니다.** 실제 형태의 생산자와 흐르는 시계를 사용해 PASS에 도달하는 실행 경로 하나를 CI에 고정합니다. 추상화한 계획 상태기계는 작은 BFS나 TLA+로 다음 두 성질을 함께 검사합니다.
   - 안전성: PASS 없이 SHIFT로 가는 경로가 없어야 합니다.
   - 도달 가능성: PASS로 가는 경로가 하나 이상 있어야 합니다.
3. **건너뛴 단계는 별도의 판정 값으로 기록하고, Executor가 승인 기록을 직접 검증하게 합니다.**
4. **설정을 Git에 두고 expand/contract 방식으로 변경합니다.** 새 이미지는 이전 설정을 받아들이고, 이전 이미지는 모르는 설정 키를 무시하게 만듭니다.
5. **Edge 실측을 가장 먼저 진행합니다.** 측정할 때는 요청마다 새 연결을 여는 방식을 사용합니다.
