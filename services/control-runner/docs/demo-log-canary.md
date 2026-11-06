# 실제 demo-chat 로그 카나리 자격

`tests/integration/verify_demo_log_canary.py`는 고정된 demo-chat revision을
읽기 전용으로 마운트한 **폐기 가능한 로컬 앱**에서 SEC-01.3의 실행 단계를
검증한다. 운영 앱·공유 DB의 설정 변경기로 사용하지 않는다.

## 실행 범위

1. 원래 시험 설정으로 앱을 띄우고 관리자 인증과 실제 PostgreSQL 연결을 확인한다.
2. 실행별 난수 카나리를 시험 앱의 `ADMIN_TOKEN` 설정에 넣는다. 앱의 실제
   `Settings.from_env`·인증 코드를 사용한다. 카나리는 폐기할 앱 안에서만 유효하다.
3. 이전 토큰 거부, 새 토큰 승인, 쓰기 차단/해제와 실제 메시지 저장 경로를 호출한다.
4. 주입한 설정이 그대로인 경우에만 원래 설정을 복원한다. 다른 변경을 덮어쓰지 않는다.
5. 복원한 앱에서 원래 토큰 승인·카나리 토큰 거부와 DB 응답을 확인한다.
6. 앱의 graceful shutdown 완료, 모든 자식 프로세스 종료, stdout/stderr 파이프의
   EOF를 모두 확인한 후에만 `logs_drained` 영수증을 만든다. sleep을 전달 완료의
   근거로 사용하지 않는다. 전체 로그는 메모리에서만 읽고 비밀 없는 판정 증거만 반환한다.

각 프로세스의 stdout/stderr를 동일한 시험 스트림에 모은다. 앱·복원 프로세스의 로그를
누락하지 않는다. 각 스트림의 대조 문자열은 고정 시험 wrapper가 출력하므로, 이것은
외부 로그 수집기가 실제 앱 요청을 전달했다는 증거가 아니다.

## 대조군

| 경로 | 기대 |
|---|---|
| 원래 앱 로그 | 카나리 원문 absent |
| 고정 wrapper가 stderr에 의도적으로 출력 | present, 1건 검출 |
| stdout 대조 문자열 생략 | UNKNOWN / POSITIVE_CONTROL_FAILED |

최신 Uvicorn은 정상 shutdown 뒤 받은 SIGTERM을 재발행한다. 종료 코드만 0으로
제한하지 않고, 직접 보낸 SIGTERM·앱 shutdown 완료·EOF를 함께 확인한다. 강제 SIGKILL,
출력 초과, 부분 수집, 복원/인증 실패에서는 성공 영수증을 만들지 않는다.

## 재현

```sh
docker build -f testing/log-canary.Dockerfile \
  -t bravo-t14-log-canary-qualification:20261106 testing
PYTHONPATH=src python tests/integration/verify_demo_log_canary.py \
  --demo-repo "$DEMO_REPO" --demo-revision "$FULL_COMMIT_SHA"
```

검증기는 image ID를 먼저 고정하고, 고유 label의 비 root 컨테이너만 생성한다.
외부 네트워크 없음·포트 미공개·capability 제거·no-new-privileges·512MiB·1CPU이며
finally에서 자기 컨테이너만 확인·삭제한다. 호스트의 기존 컨테이너와 앱은 유지한다.
이미지 빌드만 공개 패키지 저장소를 사용하며 전체 Python 버전 목록을 고정한다.

[실측 기록](../../../docs/evidence/t14-demo-log-canary-2026-11-06/README.md).

## 운영 인수에 필요한 것

- 실제 workload/설정 revision의 실행권, 승인된 설정 주입과 충돌 없는 복원
- 앱 담당이 허용한 코드 경로, 복원 실패 시 운영자 인계 및 효과 원장
- 실제 CloudWatch/클러스터 수집기의 전체 스트림 등록, flush/drain 근거와 원문 읽기 권한
- T10의 현재 계획과 결속한 저장·T19 인수

DB Secret 공급 함수는 로컬 파일로 대체하고, 첨부 저장소 호출은 사용하지 않는다.
운영 Secrets Manager/OpenBao/S3, 실제 배포 이미지, Collector 또는 CloudWatch 검증이 아니다.
검출 대상도 카나리 원문이며 인코딩·분할 값이나 실행하지 않은 경로 전체를 보장하지 않는다.
T14 양쪽 환경 완료 체크를 올리지 않는다.
