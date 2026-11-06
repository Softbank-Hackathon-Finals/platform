# T14 SEC-01.3 실제 앱의 로그 카나리 시험

2026-11-06. 실제 demo-chat revision
`4c6684e62d27ccfb6cb68b1e7ead263fd7998c79`와 PostgreSQL18.4를 폐기 가능한
로컬 컨테이너에서 실행했다. 운영 앱/DB·CloudWatch는 시험 대상이 아니다.

| 사례 | 실제 관찰 | 판정 |
|---|---|---|
| 정상 앱 인증·쓰기 차단/해제·메시지 저장 | 30개 로그, 카나리0건, 대조 스트림2개 | absent |
| wrapper의 의도적 stderr 누출 | 31개 로그, 카나리1건, 대조 스트림2개 | present |
| stdout 대조 문자열 누락 | 29개 로그, 대조 스트림1개 | UNKNOWN / POSITIVE_CONTROL_FAILED |

모든 사례에서 원래 설정·인증 복원, 실제 DB 응답, 정상 shutdown과 stdout/stderr
EOF를 확인했다. 시간 경과만으로 전달 완료를 선언하지 않았다. 카나리·DB 비밀번호·
원본 로그는 증빙에서 제외하고 판정 payload와 해시만 비공개 검증 기록에 보존했다.

최종 비밀 없는 판정 영수증 SHA-256:
`9ec6b643503f36679db67b70f28edb96169677091d9904aa20f567f0df139e96`.

고정 시험 이미지:
`sha256:7e39cb9f1712f93870f1caf4f75b58081953430dd0b8871541aaf65f9b2c24cc`.
[패키지 목록](../../../services/control-runner/testing/log-canary-requirements.txt).
네트워크 없음, 비root, 공개포트 없음, capability 제거. 생성한 컨테이너 정리 완료.

초기 실행 실패는 스키마가 기대하는 `chat_app` 역할 이름, 최신 앱의 OpenTelemetry
의존성 누락, Uvicorn 정상 종료 후 SIGTERM 재발행을 각각 확인했다. 역할·의존성을
고정하고, 실제 shutdown 완료를 종료 코드와 함께 대조하도록 보완한 후 세 경로를 통과했다.
실패 실행도 성공으로 세지 않으며 그때 생성한 컨테이너는 모두 정리했다.

[재현·한계·운영 인수](../../../services/control-runner/docs/demo-log-canary.md).
이 검증은 고정 앱의 제한된 경로와 로컬 파이프 수집이다. 양쪽 운영 로그 수집,
Secrets Manager/ESO 설정 주입, 운영 효과 원장·복구 또는 T14 전체 PASS를 의미하지 않는다.
