# 고정 입력

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

T16 계약이 확정돼 출력이 바뀌면 이 파일을 같은 방법으로 다시 만든다. `tests/test_real_t16.py`가 저장소의 T16 코드를 실제로 돌려 이 파일과 대조하므로, 어긋나면 그 시험이 실패한다.
