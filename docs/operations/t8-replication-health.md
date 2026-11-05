# T8 정방향 복제 상태: 꺼짐 원인 확인 · 설정 분리 · 지표

**구독이 꺼지면 아래 한 줄로 꺼진 시각과 오류 종류를 값 노출 없이 읽을 수 있다.** 실제 클러스터에는 아무것도 적용하지 않았다. 로그 형식과 접속 경로는 임시 k3s에 띄운 실제 CNPG 1.30.1로 확인했다.

## 꺼졌을 때 원인 확인

읽기만 한다. ENABLE이나 시험 행 쓰기를 하지 않으며, 지난 복구 승인을 대신하지도 않는다. 목적지에 `kubectl` 권한이 있으면 접속 정보 없이 시각과 오류 종류까지 나온다.

```bash
kubectl -n target-dev logs target-db-1 -c postgres --since=48h | python3 infra/envs/target-workloads/replication_incident.py
```

두 DB 접속 정보가 있으면 WAL 차이와 오류 수까지 한 번에 나온다. 값은 `resume_replication.py`와 같은 변수로 주며 `verify-full`이어야 한다. 역할은 `chat_replicator`면 된다.

```bash
kubectl -n target-dev logs target-db-1 -c postgres --since=48h | infra/envs/target-workloads/replication_incident.py
```

접속 정보가 없는 사람에게는 로그 부분만 나오고 종료 코드가 1이다. 출발지 `kubectl`이 있으면 WAL 차이는 따로 읽는다.

```bash
kubectl -n source-dev exec source-db-1 -c postgres -- psql -X -q -At -d chat -c "BEGIN READ ONLY; SELECT active, wal_status, pg_wal_lsn_diff(pg_current_wal_flush_lsn(), confirmed_flush_lsn) FROM pg_replication_slots WHERE slot_name = 't8_source_to_target'; ROLLBACK;"
```

| 출력 | 뜻 |
| --- | --- |
| `disables[].at` | 꺼진 시각. 카탈로그에는 시각이 남지 않아 로그에서 읽는다 |
| `disables[].cause` | `connection`(타임아웃 · 스트림 끊김), `conflict`(무결성 충돌), `other`, `unknown`(같은 프로세스의 오류가 로그 범위 안에 없다) |
| `disables[].event`, `sqlstate` | 고정된 분류 이름과 SQLSTATE. 로그 문장은 출력하지 않는다 |
| `latest_error`, `counts` | 꺼지지 않고 재시도 중일 때의 마지막 오류와 분류별 건수 |
| `log_window` | 읽은 로그의 범위. **`disables`가 비어 있어도 이 범위 밖의 일은 모른다** |
| `state.gap_bytes`, `gap_mib` | 출발지 WAL과 슬롯 확인 위치의 차이. 접속 정보가 있을 때만 |
| `state.forward_enabled`, `apply_workers`, `target_apply_errors`, `target_conflicts` | 지금 상태와 누적 수 |

`connection`은 그 시각에 연결이 끊겼다는 뜻까지만이다. 왜 끊겼는지(Tailscale 경로, 노드 재시작 등)는 이 명령이 말해 주지 못한다. Pod가 재시작했다면 `--previous`로 이전 컨테이너 로그도 읽는다. `conflict`면 다시 켜도 또 실패하므로 쓰기 경로부터 본다.
