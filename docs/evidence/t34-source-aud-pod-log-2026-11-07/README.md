# T34 출발지 AUD-01.1: CNPG primary 자체 로그 검색 실측

2026-11-07 20:42 KST, 실제 출발지(AWS EKS, `source-dev`)에서 실행했다.
- 경로: 출발지 EKS API는 비공개 엔드포인트만 연다. 그래서 `infra/envs/source-eks/README.md`의 운영자 경로를 썼다. 플랫폼 호스트를 거치는 SSM 원격 포트포워딩에, EKS CA와 원래 TLS 서버 이름을 그대로 둔 비공개 kubeconfig를 붙였다.
- 신원: 운영자 본인 SSO 세션
- 도구: `operations/controls/run.py`의 `pod-log` 어댑터([ADR-0037](../../adr/0037-aud-login-pod-log-source.md))

**중앙 감사 로그 검색이 아니다.** T5 기준은 출발지 CNPG 로그를 CloudWatch Logs에서 검색하는 것인데, 이번에는 DB 인스턴스 자기 로그(`pods/log`)를 읽었다. 이 실행은 확인용이고 GATE에 기록하지 않았다. [목적지 실측](../t34-target-aud-pod-log-2026-11-07/README.md)과 같은 방식이다.

| 항목 | 결과 |
|---|---|
| 카나리 | `source-db` primary Pod 로컬 소켓, `postgres` peer 로그인, `application_name=<표식>`, `rolsuper=true` 확인. 비밀번호 · Secret 값 사용 없음 |
| 검색 | 같은 컨테이너 로그에서 표식 줄 2개 검색, `connection authorized` 1개 일치 |
| 판정 | `AUD-01.1` **present → PASS**, 실행 8초(EKS 토큰 발급 포함) |
| 나머지 시험 | 이 실행에 등록하지 않은 IAM-01.1 · SEC-01.1은 UNKNOWN(NOT_RUN) |

원문, 비공개 kubeconfig, 엔드포인트 주소는 운영자 비공개 폴더에 둔다. kube context, Pod UID, 표식, 계정 식별자는 공개하지 않는다.
