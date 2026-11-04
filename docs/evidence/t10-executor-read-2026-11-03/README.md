# T10 Executor 읽기 어댑터 검증 — 2026-11-03

새 명령을 보내지 않고 T12 실행을 재조회하는 소비 단위다. 실제 Executor 서비스 배포나 컷오버 인수 결과가 아니다. [소비 계약](https://github.com/Softbank-Hackathon-Finals/platform/blob/ed7b54084a9a6f25248d07fa100dc4404f88104a/docs/contracts/T10-executor-read-v1-proposal.md)에서 입력 신뢰 경계와 후속 작업을 설명한다.

| 검증 | 결과 | 입증 범위 |
|---|---|---|
| 새 조회 시험 | 89 passed | 5개 원격 상태 보존, 응답 유실 조회, ID·계획·epoch 대조, 잘못된 형식·부정 경로, 이력 회귀, 정보 가림 |
| 기존 저장소·API 포함 전체 T10 | 248 passed | 로컬 SQLite와 실제 임시 PostgreSQL, 기존 원자 저장·재시작·권한 회귀 포함 |
| 공개 T12 API 호환 | 9/9 | #116 `cccddc0`의 실제 FastAPI GET 처리기 + loopback HTTP. 저장소는 모의 객체 |
| AWS EC2 격리 실행 | 89 passed | 동일 소스·시험을 Python 3.12.3 비관리자 프로세스와 loopback 서버에서 실행 |

AWS 실행은 **2026-11-03 21:27:56–21:28:04 KST**에 SSM 성공·종료 코드 0으로 끝났다. 계정·인스턴스 소유 경계를 먼저 대조했다. 실행 파일 해시는 [aws-test.json](aws-test.json), 공개 T12 API 파일 해시는 [published-api.json](published-api.json)에 기록했다. 원시 SSM 응답과 운영 식별자는 비공개로 보존했다.

처음 두 AWS 시도는 시험 전 준비 단계에서 중단됐다. 첫 시도는 제한된 PATH에서 `runuser`를 찾지 못했고, 두 번째는 기본 Python의 pip 포함 venv 준비가 실패했다. 세 번째는 subprocess의 사용자 지정 기능과 pip 없는 임시 venv, PyPI의 고정 pip wheel 및 배포 해시 검증을 사용했다. 네 번째 시도는 합성 UUID를 공개 식별자 검사와 충돌하지 않는 형태로 바꾼 시험 파일을 재검증했다. 독립 검토에서 느린 분할 응답의 시간 검사가 8KiB 버퍼 뒤로 지연되는 P2를 발견해 수신 조각마다 검사하도록 수정했고, 다섯 번째 시도에서 실제 TCP 회귀를 포함한 최종 89개 시험을 다시 통과했다. 전역 패키지·호스트 설정을 바꾸지 않았고 최종 임시 디렉터리 제거를 확인했다. 실패 시도를 시험 통과 횟수에 포함하지 않았다.

## 재현

```sh
cd services/platform-events
uv sync --locked
uv run ruff check .
uv run pytest tests/test_executor.py -q
uv run pytest -q
```

전체 회귀는 PostgreSQL 서버 도구가 필요하다. 새 조회 시험은 기존 DB를 사용하지 않으며 loopback에서 임시 포트를 선택해 HTTP 서버를 열고 닫는다. AWS 검증도 운영 DB·Kubernetes API·실제 Executor를 호출하지 않았다. 별도 클라우드 자원을 만들지 않았다.

## 결과를 해석하는 경계

- `OBSERVED`는 조회한 응답의 동일성과 형식을 확인했다는 뜻이다. 원격 `DONE`을 T10 완료로 전이시키지 않는다.
- 모의 저장소를 쓴 공개 API 호환 검사는 실제 T12 DB의 lease·epoch·동시 실행 계약을 다시 입증하지 않는다.
- AWS에서의 loopback 시험은 플랫폼→양쪽 클러스터 네트워크, 실제 서비스 TLS·인증, Executor 배포를 검증한 것이 아니다.
- 승인 소비·불변 명령과 실행 ID의 영속 결속, 단계별 SSE projection, 실제 DB 쌍 동일성, T13 무결성 비교·앱 재접속을 포함한 컷오버는 후속 통합 조건이다.
- T7 검증기·T12 실행기의 다른 PR은 수정하지 않았다. 이 증빙은 해당 PR의 차단 리뷰를 해소하지 않는다.
