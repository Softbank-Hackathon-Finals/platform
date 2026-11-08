# SQS → Lambda 호환 런타임 (T46)

큐에서 메시지를 꺼내, AWS Lambda용으로 쓴 핸들러를 고치지 않고 AWS SQS 이벤트 형식으로 부른다. 앱은 `AWS_ENDPOINT_URL_SQS`만 목적지 큐 서버(ElasticMQ, ADR-0046)로 바꾼다. 구조는 ADR-0045, 컷오버 · 되돌리기 규칙은 ADR-0047. 판정 · 실행 경로라 LLM을 쓰지 않는다.

| 모듈 | 부품 | 하는 일 |
|---|---|---|
| `queue.py` | ① 큐 연결 | 꺼내기 · 지우기 · 속성. 2단계에서 교체 |
| `invoke.py` · `_child.py` | ② 호출 관리 | 하위 프로세스 그룹으로 호출하고 끝나면 그룹째 종료(손자 프로세스 포함), 출력은 파일로 받아 끝 4000바이트만, 파일 크기 상한 16MiB. 2단계에서도 그대로 |
| `event.py` | ③ 메시지 형식 | SQS 이벤트 `Records[]` · context. 2단계에서 교체 |
| `runner.py` | 반복 | 성공하면 배치 삭제, 실패 · 시간 초과 · 비정상 종료면 남겨 재전달 · 큐의 실패 보관 큐 설정에 맡김 |
| `admin.py` | 정지 스위치 | Executor의 demo-chat 계약과 같은 `/admin/write-block`, 처리 중 수 반환 |
| `config.py` · `cli.py` | 설정 | 지원하지 않는 설정, 큐 가시성 시간, 핸들러로 실행기 비밀(`AWS_*` · `ADMIN_TOKEN`) 전달을 시작 전에 거부(`check`), 실행(`run`) |
| `source.py` | 출발지 대조 | 출발지 이벤트 소스 매핑 · 함수 설정(AWS API 출력 JSON)과 비교. 필터 · 배치 일부 실패 응답 · 배치 대기 · 레이어 · 다른 배치 크기 · 제한 시간 · 핸들러를 거부 |

```sh
uv sync --locked && uvx ruff check . && uv run --locked pytest -q
uv run bravo-function-runtime check --config runtime.json --source <출발지 설정 디렉터리>   # 지원 안 함이 있으면 종료 코드 2
```

이미지는 develop에 병합될 때 `publish-function-runtime-image` 워크플로가 `ghcr.io/softbank-hackathon-finals/platform-function-runtime:sha-<커밋>`으로 발행한다(빌드 한 번 → 취약점 검사 → 같은 바이트 발행 → digest 확인). 핸들러를 얹은 함수 이미지는 핸들러가 있는 앱 레포가 이 베이스의 digest 위에 만들고, 핸들러 의존성은 Lambda zip처럼 `/var/task`에 넣는다.

설정 예와 로컬 증명은 [`testing/local`](testing/local/README.md), 큐 서버 후보 비교는 [`testing/queue_compare`](testing/queue_compare/README.md).

지원 범위는 SQS 표준 큐 → Python 핸들러다. FIFO · 이벤트 필터 · 배치 일부 실패 응답 · 배치 대기 · 자동 확장은 재현하지 않고 거부하거나 고정값으로 둔다.
