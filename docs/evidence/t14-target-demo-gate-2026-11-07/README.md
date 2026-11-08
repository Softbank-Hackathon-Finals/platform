# T14 목적지 demo-gate:1 실제 실행: IAM-01.1 FAIL(의도한 결함) · AUD-01.1 PASS

2026-11-07 20:52 KST, 실제 목적지(온프렘 RKE2, `target-dev`)에서 `operations/controls/run.py`로 실행했다(4초). 운영자 Mac에서 사람 tailnet의 목적지 operator API 프록시와 `kubectl port-forward svc/seaweedfs 18333:8333`을 썼다. **GATE에는 기록하지 않았다.**

| 시험 | 어댑터 | 결과 | 관찰 | 대조군 |
|---|---|---|---|---|
| IAM-01.1 | `s3-endpoint` | **FAIL** | 익명 GET **허용**(allow) | 앱 `client` GET 성공(pass) |
| SEC-01.1 | — | UNKNOWN(NOT_RUN) | 등록 안 함. T9 `t14-other` SA · OpenBao role 대기 | — |
| AUD-01.1 | `pod-log` | **PASS** | 관리자 로그인 기록 있음(present) | 해당 없음 |

- **IAM-01.1 FAIL은 의도한 상태다.** T44 원하는 상태의 `target-object-exposure`가 `open-demo`(데모 1번 결함, 버킷 익명 읽기)다. 데모 각본대로 S1 GATE는 이 시험 때문에 BLOCK이다. S2 변경(닫기)과 T31 반영 뒤 같은 시험이 PASS로 바뀌어야 한다.
- 자격과 객체:
  - 앱 자격: `target-app-objects` Secret에서 실행 환경변수로만 넘겼다. 파일 · 로그 · 증거에 값이 없음을 확인했다. `target-seaweedfs-auth`(관리 키 포함)는 쓰지 않았다.
  - 카나리 객체 `attachments/t14-canary.txt`: 이미 있어서 아무것도 쓰지 않았다.
- AUD-01.1은 [ADR-0037](../../adr/0037-aud-login-pod-log-source.md) 경로다(CNPG primary 자체 로그, 중앙 검색 아님).
- 원문은 운영자 비공개 폴더(0600)에 있다.

출발지 같은 실행은 [세 시험 PASS](../t14-source-demo-gate-2026-11-07/README.md)다.

## 추가 실행: SEC-01.1 포함 (22:55 KST)

T9 OpenBao 비교 역할 `t14-other`(#707, 22:0x 적용)가 생긴 뒤 같은 도구로 세 시험을 다시 실행했다(5초). 실행 환경과 앱은 등록값(`bravo-target-dev` / `bravo-target-collector`, 워크로드 ID)이다.

| 시험 | 결과 | 관찰 | 대조군 |
|---|---|---|---|
| IAM-01.1 | **FAIL** | 익명 GET 허용(open-demo, 의도한 결함) | 앱 GET 성공 |
| SEC-01.1 | **PASS** | `t14-other` 읽기 거부(403) | 앱 신원(`target-app-reader` · SA `target-eso-reader`) 읽기 성공, 현재 버전 일치 |
| AUD-01.1 | **PASS** | 관리자 로그인 기록 있음 | 해당 없음 |

- 운영자 Mac → `kubectl port-forward svc/target-openbao 18200:8200`, `server_hostname` `target-openbao.bravo-secrets.svc`로 TLS 확인(ConfigMap `target-openbao-ca`).
- 두 SA 토큰은 `kubectl create token --audience openbao --duration 10m`, 0600 파일. OpenBao 토큰은 수집기가 끝나면 revoke-self.
- 첫 실행(22:53)은 SEC-01.1이 UNKNOWN(UNEXPECTED_RESPONSE)이었다. 실제 OpenBao 2.x가 KV 읽기 거부를 multierror 형식(`"1 error occurred:\n\t* permission denied\n\n"`)으로 돌려주는데, 수집기가 `"permission denied"` 한 형식만 받았다. 두 형식만 정확히 받도록 고친 뒤 다시 실행했다(이 PR).
- 증거에는 비밀 값 · 토큰이 없다(SEC 증거는 단계 결과와 해시만).

## 앱 쪽 신원을 `demo-chat`으로 (23:21 KST)

#741로 앱 자기 SA `demo-chat`에 DB 경로 하나만 읽는 OpenBao 역할 `demo-chat`이 생겨서, SEC-01.1의 앱 쪽 신원을 ESO 읽기 계정(`target-eso-reader`)에서 `demo-chat`으로 바꿔 다시 실행했다. ESO 읽기 계정은 객체 · 관리자 경로도 읽으므로 앱 신원으로는 넓다. 결과는 같다: IAM-01.1 FAIL(open-demo) · **SEC-01.1 PASS**(`demo-chat` 읽기 성공, `t14-other` 403) · AUD-01.1 PASS. GATE 기록에는 이 실행을 쓴다.

