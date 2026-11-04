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

통과 시 namespace UID와 시험 실행 ID를 출력한다. 시험 실패·확인 불가·UID 변경·시험용 자원 정리 미확인은 BLOCK이며 종료 코드 1이다. 정리는 최대 30초 기다린다. 모의 리허설 흐름 시험은 격리 실패·불명·UID 변경·시험 자원 누수 시 모의 Job이 호출되지 않고, 성공 시 시험한 namespace UID가 모의 Job으로 전달되는지 확인한다. 이 모의 흐름은 테스트 코드 안에만 있으며 실제 러너를 연결하지 않는다. 이 결과는 검증 Job 실행이나 승인 권한이 아니다. T38 연결 시 Job과 네임스페이스 정리는 같은 UID에 결속하고, T10 실행 권한을 별도로 확인해야 한다.

## 버킷 읽기 관측 프로토타입

`bucket_task.prepare_bucket_policy_task`는 원본 정책 원문·SHA-256·실행 ID·시험 버킷을 담은 번역 작업 입력을 만든다. 중복 JSON 키나 범위 밖 입력은 거부한다. 현재 모델 도구 및 T38에는 연결되지 않았으며 SeaweedFS 정책을 생성하거나 적용하지 않는다.

`bucket_probe.observe_bucket_reads`는 신뢰된 호출자가 제공한 인증·무인증 읽기 어댑터로 같은 시험 객체를 인증 → 무인증 → 인증 순서로 관측한다. 기존 객체의 양성 대조와 본문 SHA-256이 모두 일치해야 무인증 403을 해석할 근거가 생긴다. 이 함수의 결과는 관측값이며 T38 영수증이나 승인 판정이 아니다. 테스트는 모의 어댑터만 사용한다.

실제 연결 전에 T9와 시험 전용 버킷·객체·자격 및 지원 정책 형식을, T38과 독립 검증 Job의 인증된 관측 전달 및 비공개 시험 격리 방식을 확인해야 한다. 정책 번역, 실제 SeaweedFS 시험, 버킷 삭제 명령 차단·감사, 정리 및 승인 결속은 아직 구현하지 않았다. T9 개발용 SeaweedFS 구성은 S3 IAM 모드를 끈 상태이므로 임의의 AWS 정책이 적용된다고 가정하지 않는다.

## 버킷 삭제 요청의 현재 차단 경계

현재 T11은 `rehearsal.exec`를 제공하지 않으므로 요청 자체를 거절하고 `TOOL_NOT_ALLOWED`로 기록한다. 제안 내용의 `delete_bucket`도 T11 정책으로 거부한다. 다음 검사는 실제 T11 도구 계층을 호출하지만 버킷 삭제 API나 클러스터를 호출하지 않는다.

```bash
cd ../agent-worker
uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_delete_boundary.py
```

현재 기록은 도구 미허용 또는 제안 거부라는 일반 사유다. 삭제 시도라고 식별되는 감사 이벤트, 향후 허용될 `rehearsal.exec`의 실행 경계, 감사 로그 저장·콘솔 표시는 확인되지 않았다. 해당 계약은 T11·감사 경로 담당자와 확인해야 한다. 이 검사는 T15 버킷 삭제 차단 데모 인수 증거가 아니다.

## 검증 영수증과 재시도 모의 시험

아래 시험은 실제 T11 `Coordinator`에 모의 provider와 영수증을 전달한다. 세 번의 검증 실패, 실패 후 성공, UNKNOWN·효과 불명·실행 정보 불일치, 실패 증거 불일치와 중복 영수증을 확인한다.

```bash
cd ../agent-worker
uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_retry_flow.py
```

이 시험은 프로세스 메모리에서만 실행한다. T38의 실제 두 번째 이후 검증, 영수증 인증·증거 대조, T10의 실행 잠금과 시도 횟수 영속화 또는 재시작 복구는 증명하지 않는다.

## 승인 결속 모의 시험

T10 이벤트 스키마와 모의 수신기로 diff·이미지 digest가 현재 값과 다른 경우와 검증·정리 미확인 경우를 시험한다. 모의 수신기는 테스트 코드에만 있고 실제 승인 경로가 아니다. 이벤트 스키마 통과는 현재 값 일치나 실행 권한의 증명이 아니다.

```bash
cd ../platform-events
uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_approval_binding.py
```

T10 변경 승인 경로, diff 계산 방식, 검증한 제품 이미지와 승인 digest의 신뢰된 결속은 담당자와 확인이 필요하다. 이 모의 시험은 영수증 진위, 이미지 빌드·실행·정리 또는 실제 승인 전달을 증명하지 않는다.

## 자격증명 코드 수정 작업 입력과 모의 시험

`secret_task.prepare_credential_migration`은 demo-chat의 원본 revision과 `app/db_secret.py`, `app/config.py`, `app/main.py`를 대상으로 `/run/bravo/database/credentials.json` 주입 방식의 변경 작업 입력을 만든다. 기존 자격 캐시 및 인증 실패 후 재조회 유지와 값 비노출을 요구한다. 파일 경로와 JSON 키는 `infra/envs/target-secrets/README.md`의 현재 소비 계약을 따른다.

`tests/test_secret_simulation.py`는 가짜 전달자와 DB를 사용하며 실제 demo-chat 수정안 실행이나 Kubernetes RBAC·OpenBao·ESO·DB 연결의 증거가 아니다. T11 제안서·스냅샷과 T38 독립 검증 Job, 실제 Secret 전달·접근 거부 및 자격 회전 검증은 후속 통합으로 남는다.

## 정리와 재시작 경계 시험

아래 시험은 T38의 기존 `Kube.cleanup`에 모의 Kubernetes API 응답을 주어 생성 UID 조건, 삭제 후 부재 확인, 다른 UID의 네임스페이스 발견, 삭제·조회 실패와 시간 초과를 확인한다. 기존 Coordinator를 새로 생성하면 시도 이력이 복구되지 않는다는 한계도 확인한다.

```bash
cd ../agent-worker
uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_cleanup_recovery.py ../rehearsal-runner/testing/test_retry_flow.py
```

모의 API 시험은 실제 네임스페이스 삭제·부재나 T38 제품 리허설 연결을 증명하지 않는다. 재시작 후 실행 잠금·시도 수·영수증 복구와 원격 효과 재조회는 T10/T38 계약 및 실제 구현이 필요하다. 확인 불가 상태에서 승인을 전달하지 않아야 한다.
