# T8 복제 상태 측정 (2026-11-06)

해석과 절차는 [운영 문서](../../operations/t8-replication-health.md)에 있다. 전부 임시 환경의 대역이고 실제 AWS · VM · Tailscale 경로는 재지 않았다.

| 파일 | 내용 |
| --- | --- |
| `lab.json` | `subscription_health_lab.py`: 임시 PostgreSQL 18.4 두 대, 가정한 netem 100Mbps · RTT 30ms, 조건마다 3회 |
| `cnpg.json` | `subscription_health_cnpg.py`: 임시 k3s 한 노드의 실제 CNPG 1.30.1, T6 · T30 렌더, walsender 정지로 끊김을 대신함, 1회 |
| `source-manifest.json` | 측정 코드가 들어간 커밋의 파일 해시 |

**측정한 코드와 커밋의 차이.** 측정은 커밋 전 작업 트리에서 돌렸다. 그 뒤 바뀐 것은 다음뿐이다. 쿼리와 시험 절차는 그대로다.

- `replication_incident.py`: 상태 읽기를 방향별로 나누고 57P01을 오류에서 뺐다. 한쪽 접속 실패는 임시 PostgreSQL로 다시 확인했다(출발지 `OperationalError`, 종료 코드 1).
- `render.py`, `subscription_health_lab.py`: 주석만 고쳤다.
- `subscription_health_cnpg.py`: 내려받기 제한 시간, 진단 명령 호출 횟수, 구독 이름의 출처만 고쳤다.

머신 부하는 다른 세션과 함께 쓴 값으로, `lab.json`의 `machine`에 있다. CNPG 실험은 앞선 실행에서 exporter 캐시를 몰라 지표 변화를 놓쳐, 지연을 재도록 고쳐 다시 돌린 결과만 남겼다.
