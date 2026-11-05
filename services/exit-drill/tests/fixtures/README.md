# 고정 입력

`inventory-t16-shakedown.json`은 T16 #371의 `65b28b5d561976139378dd97eaa43c0246255132`가
같은 합성 입력에서 생성한 최신 계약 대조용 문서다. 워크로드 설정이 가리키지 않는
감사 버킷을 배포 전용 의존성으로 분리하므로 항목은 6개이며, 지도에서 이 버킷과 KMS는
계속 `unknown`이어야 한다. `tests/test_real_t16.py`는 이 문서와 현재 T16의 실제 출력을 대조한다.
아래 명령에 `--repo-ref tests/fixtures/demo-chat`을 추가했으며, 날짜는 기존 고정 입력과
동일하게 고정했다. 입력은 저장소의 합성 값이고 새 문서에 사후 가리기는 하지 않았다.
이전 `inventory-demo-chat.json`은 기존 5개 항목 문서에 대한 소비자 회귀용으로 보존한다.

`inventory-demo-chat.json`은 T16 코드(PR #144, `origin/feature/T16-inventory` `3b299d8`)가 자기 시험 입력으로 실제 출력한 문서다. 손으로 쓴 값이 아니다.

```sh
uv run bravo-inventory scan --app-id demo-chat --repo tests/fixtures/demo-chat --app-path app \
  --source-env aws-seoul --namespace chat --exports tests/fixtures/exports/aws \
  --profile profiles/onprem-rke2.json --profile profiles/onprem-minimal.json \
  --target-exports onprem-demo=tests/fixtures/exports/onprem --output inventory.json
```

세 군데만 고치고 T16과 같은 방식으로 `digest`를 다시 계산했다.

- `app.repo`의 절대 경로에 사용자 폴더가 들어가므로 상대 경로로 바꿨다.
- S3 ARN의 버킷 이름과 KMS 키 ID를 `scripts/public_evidence.py`가 허용하는 예시 값으로 바꿨다. 원래도 T16 시험 입력의 가짜 값이지만 실제 식별자와 구별되지 않는다. T27은 이 값들을 읽지 않는다.

계정 번호 `000000000000`은 T16 시험 입력의 값 그대로다.

T16 출력이 바뀌면 최신 계약 대조용 문서를 같은 방법으로 다시 만든다. `tests/test_real_t16.py`가 저장소의 T16 코드를 실제로 돌려 대조하므로, 어긋나면 그 시험이 실패한다.
