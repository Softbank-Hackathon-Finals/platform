# T8 복제 시간 초과 측정 (2026-11-06)

해석과 절차는 [운영 문서](../../operations/t8-replication-health.md#116-새벽-끊김-원인과-대응)에, 결정은 [ADR-0030](../../adr/0030-t8-replication-timeout.md)에 있다.

| 파일 | 내용 |
| --- | --- |
| `incident.json` | 실제 두 CNPG 인스턴스의 로그와 설정을 읽기만 해서 남긴 값. 시각(UTC) · 고정 라벨 · SQLSTATE · walsender 세션 길이 · 설정값만 있다. 로그 문장 · 주소 · 역할 · 행 값은 없다 |
| `lab.json` | `replication_timeout_lab.py`: 임시 PostgreSQL 18.4 두 대, 가정한 netem 100Mbps · RTT 90ms, 조건마다 3회 |
| `source-manifest.json` | 측정 코드가 들어간 커밋의 파일 해시 |

**실제 환경에서 한 일은 읽기뿐이다.** `kubectl logs --since=12h`와 `READ ONLY` 트랜잭션의 `pg_settings` 조회다. 설정 변경 · ENABLE · 쓰기는 하지 않았다. 원본 로그는 저장소 밖에만 두었다.

**lab의 정체는 대역이다.** 10초 동안 양방향에 한 방향 지연을 2 · 4 · 6초 더했고 패킷은 버리지 않았다. `tc qdisc change`라 큐에 있던 패킷은 정체가 끝날 때 그대로 나간다. 실제 DERP 중계의 정체가 이런 모양인지는 모른다. 단절(`cut_20s`)만 패킷을 모두 버렸다.

**경로 확인.** 시작할 때 잰 링크는 왕복 110.9ms, `iperf3` 3초 처리량 6.1Mbps였다. 처리량은 설정한 100Mbps에 못 미치지만, 쓰기 50건/초(약 0.7Mbps)에는 충분하다.

**시간 초과는 워커를 띄운 채 바꿨다.** `ALTER SYSTEM`과 reload로 5초와 30초를 번갈아 넣었고, 회차마다 두 값을 다 썼다. reload 6회 모두 적용 워커와 walsender의 PID가 그대로였고 `pending_restart`는 0이었다(`lab.json`의 `reloads`).

**측정한 코드와 커밋의 차이.** 측정은 커밋 전 작업 트리에서 돌렸고, 그 코드는 `source-manifest.json`의 커밋(`5fc6bdc`)에 들어 있다. 측정을 시작한 뒤 바뀐 것은 `replication_lag.py`의 주석 한 곳뿐이다.

머신 부하는 다른 세션과 함께 쓴 값으로, 시행마다 `lab.json`의 `trials[].machine`에 있다.
