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
