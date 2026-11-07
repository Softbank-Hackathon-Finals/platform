# T34 목적지 AUD-01.1: CNPG primary 자체 로그 검색 실측

2026-11-07 20:30 KST, 실제 목적지(온프렘 RKE2, `target-dev`)에서 실행했다. 운영자 Mac에서 사람 tailnet의 목적지 operator API 프록시를 거쳤고, `operations/controls/run.py`의 `pod-log` 어댑터를 썼다([ADR-0037](../../adr/0037-aud-login-pod-log-source.md)).

**중앙 감사 로그 검색이 아니다.** DB 인스턴스가 쓰는 자기 로그(`pods/log`)를 읽었다. T5 기준(CloudWatch · 클러스터 로그 수집기)보다 약한 원천이다. 한계는 ADR-0037에 있다. 이 실행은 확인용이고 GATE에 기록하지 않았다.

| 항목 | 결과 |
|---|---|
| 카나리 | primary Pod 로컬 소켓, `postgres` peer 로그인, `application_name=<표식>`, `rolsuper=true` 확인. 비밀번호 · Secret 값 사용 없음 |
| 검색 | 같은 컨테이너 로그, 카나리 시작 1초 전부터. 표식 줄 2개 검색, 그중 `connection authorized: user=postgres database=chat …` 1개 일치 |
| 판정 | `AUD-01.1` **present → PASS**, 수집 3.9초(카나리 시작부터 수집까지 약 4초) |
| 완전성 | 컨테이너가 카나리 전부터 실행 중, 읽기 전후 Pod UID · 재시작 수 · 시작 시각 같음, 바이트 상한 미도달 |
| 나머지 시험 | 이 실행에 등록하지 않은 IAM-01.1 · SEC-01.1은 UNKNOWN(NOT_RUN) |

원문(`control-run.json`, 증거 파일)은 운영자 비공개 폴더(0700/0600)에 둔다. 공개하지 않는 값은 다음과 같다.
- kube context
- Pod UID
- 표식
- 원천 결속 문자열(증거에는 그 SHA-256만 있다)

실행한 코드는 이 PR의 `services/control-runner/src/bravo_controls/pod_log_audit.py`와 `operations/controls/run.py`다.
- control-runner 시험 1600개 통과
- 변이 8개 모두 잡힘: 재시작 · 시작 시각 · 창 · 시각 없는 줄 · 바이트 상한 · 관리자 확인 · 재검색 · 증거 중복
