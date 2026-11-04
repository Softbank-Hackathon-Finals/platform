# rehearsal-runner

리허설 러너(T15). 지금은 리허설 네임스페이스 템플릿과 격리 시험(T3)만 있다. 결정과 실측 결과는 [ADR-0002](../../docs/adr/0002-rehearsal-egress-allowlist.md) · [ADR-0003](../../docs/adr/0003-rehearsal-namespace-isolation.md).

## 바로 해 보기 (k3d, 이 폴더에서)

```bash
scripts/k3d-up.sh                                   # 시험 클러스터 (Docker 필요)
kubectl --context k3d-bravo-rehearsal apply -f testing/standins.yaml   # SeaweedFS · OpenBao 대역 서버
k3d image import busybox:1.36 -c bravo-rehearsal    # 시험 이미지 미리 넣기
bin/create-and-check rehearsal-demo-1 profiles/k3d.env --context k3d-bravo-rehearsal
testing/mutation-check.sh profiles/k3d.env --context k3d-bravo-rehearsal   # 시험이 뚫림을 잡는지
scripts/k3d-up.sh down
```

`create-and-check` 종료 코드: 0 통과 · 1 격리 실패 · 2 시험 불가. 보고서는 `isolation-report-<ns>-<실행 ID>.json`(또는 `--report`).

## 구성

| 경로 | 내용 |
|---|---|
| `templates/` | 리허설 네임스페이스 템플릿. `${NAMESPACE}`와 목적지 프로필 값을 채운다 |
| `profiles/<환경>.env` | 목적지 프로필: 리허설에서 접속을 허용할 SeaweedFS · OpenBao의 네임스페이스 · Pod 라벨 · 컨테이너 포트 |
| `src/rehearsal_runner/isolation/` | 격리 시험. 시험 ID(P1~P4 대조, N1~N13 격리)는 ADR-0003 표와 같다 |
| `testing/` | k3d 대역 서버, 변이 시험 |

## 검사

```bash
uv sync && uvx ruff check . && uv run pytest
```

## T15 격리 시험 연결

이미 생성한 리허설 네임스페이스를 시험하려면 다음 명령을 사용한다.

```bash
uv run python -m rehearsal_runner.isolation_cli --namespace rehearsal-demo-1 --profile profiles/k3d.env --context k3d-bravo-rehearsal
```

통과 시 namespace UID와 시험 실행 ID를 출력한다. 시험 실패·확인 불가·UID 변경·시험용 자원 정리 미확인은 BLOCK이며 종료 코드 1이다. 정리는 최대 30초 기다린다. 이 결과는 검증 Job 실행이나 승인 권한이 아니다. T38 연결 시 Job과 네임스페이스 정리는 같은 UID에 결속하고, T10 실행 권한을 별도로 확인해야 한다.
