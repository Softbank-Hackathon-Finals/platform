# T14 GATE 기록용 실행: 새 계획(demo-gate:1, 환경 `env-aws` · `env-onprem`)

2026-11-08 03:42 KST, 실제 출발지(EKS)와 목적지(온프렘 RKE2)에서 `operations/controls/run.py`로 한 번씩 실행했다. 대상 계획은 `plan-ffab131f364c79ed2c50594b15a1a8e2`(03:34 등록, `demo-gate:1`, 재시험 `apply-recheck:1`)이다. 이 계획에서 환경 ID가 Executor의 환경 연결과 같은 `env-aws` · `env-onprem`으로 바뀌어서, 11-07 실행([출발지](../t14-source-demo-gate-2026-11-07/README.md) · [목적지](../t14-target-demo-gate-2026-11-07/README.md))을 새 ID로 다시 만들었다. **아직 GATE에 기록하지 않았다**(아래 "기록 전 확인").

| 쪽 | 환경 | IAM-01.1 | SEC-01.1 | AUD-01.1 | ref(`run_id`) | digest |
|---|---|---|---|---|---|---|
| 출발지 | `env-aws` / `bravo-source-collector` | **PASS** | **PASS** | **PASS** | `1b705acb-f32b-4ed3-8116-fcb1b71ec466` | `3507e29dfd2435cd6cf6199d5803de2116b24953e2b49709c9382f60ba6a0797` |
| 목적지 | `env-onprem` / `bravo-target-collector` | **FAIL**(open-demo) | **PASS** | **PASS** | `30ca6bfe-01d9-4370-8033-40259174038e` | `f21048270946fca701246150d7e49fceb4fd2e6be04630b5c6cde6619037475a` |

- digest는 `control-run.json`의 정규 JSON(`sort_keys`, 구분자 `(",", ":")`, `ensure_ascii=False`)의 SHA-256이다.
- 계획 값은 플랫폼 DB에서 읽기 전용 트랜잭션으로 확인했다. 계획 상태는 COLLECT v1이다. 환경의 `connection_ref`는 11-07과 같고, 바뀐 것은 환경 ID와 워크로드 ID뿐이다. 등록 파일 사본에서 `app`과 `environment.id`만 바꿨다.
- 실행 코드는 develop `bc27570e`이다. 11-07 실행 뒤 바뀐 것은 ENC-01.1 어댑터(#758)뿐이고, demo-gate의 세 시험과는 관계없다.
- 절차는 11-07과 같다.
  - 출발지: SSM 포트포워딩으로 EKS 사설 엔드포인트에 닿았고, 등록 클러스터 UID가 일치함을 확인했다.
  - 목적지: tailnet operator API 프록시로 들어가 SeaweedFS · OpenBao port-forward를 열었다.
  - SA 토큰은 10분짜리이고, 앱 객체 키는 실행 환경변수로만 넘겼다. 실행 뒤 터널을 닫고 토큰 파일을 지웠다.
- 목적지 IAM-01.1 FAIL은 의도한 결함(T44 `target-object-exposure`의 `open-demo`)이다. S1 GATE는 이 시험 때문에 BLOCK이어야 한다.

원문(`control-run.json`, 증거 파일)은 운영자 비공개 폴더(0600)에 있다. 공개하지 않는 값은 다음과 같다: kube context, 클러스터 · Pod UID, AUD 표식, 원천 결속 문자열(증거에는 해시만 있다).

## 기록 전 확인

같은 계획에 03:36 KST에 이미 run 두 건이 기록돼 있다. 초기 맥락 바인딩, T19 서명 게시(context · run · readiness), gate-result까지 있다. 내용은 `operations/gate/run.py subjects`와 플랫폼 DB 읽기 전용 조회로 확인했다. 그 run은 위 실행과 다르다.

| | 이미 기록된 run(03:36) | 이 실행(03:42) |
|---|---|---|
| 환경 ref | `aws:eks/bravo-source-runtime/source-dev`, `onprem:<클러스터 UID>/target-dev` | 등록 `connection_ref`(`bravo-source-collector` · `bravo-target-collector`) |
| 시험 | SEC-01.1~01.4, ENC-01.1, ENC-02.1 | IAM-01.1, SEC-01.1, AUD-01.1(demo-gate:1) |
| 결과 | SEC-01.1 PASS, 출발지 ENC-01.1 PASS, 나머지 UNKNOWN | 위 표 |

맥락이 이미 묶여 있어서, 이 실행을 지금 기록하면 맥락이 바뀌거나 무시될 수 있다. 기록 주체(`gate-operator-bravo`)에게 이 계획의 `gate:record`가 들어오고, 이미 기록된 run을 둘지 바꿀지 정해진 뒤에 기록한다.

```sh
uv run --directory services/control-gate python ../../operations/gate/run.py record \
  --plan-id plan-ffab131f364c79ed2c50594b15a1a8e2 --context <플랫폼 context> --side source --run <비공개>/run-source-ffab-1/control-run.json
# --side target 도 같은 방식
```
