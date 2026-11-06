# T34 실제 OTel CloudWatch exporter 자격 시험

2026-11-06 12:21 KST, 실제 OTel Contrib 0.164.0과 합성 CloudWatch 프로토콜 서버를
외부망 없는 전용 Docker 네트워크에서 실행했다. **AWS CloudWatch 실서비스, 운영
CNPG, IAM·KMS 적용, T14/T19 양쪽 PASS 검증이 아니다.**

- Collector: `otel/opentelemetry-collector-contrib@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b`
- 로컬 fixture 이미지 ID: `sha256:7e39cb9f1712f93870f1caf4f75b58081953430dd0b8871541aaf65f9b2c24cc`
- 비공개 영수증 SHA-256: `48d45476a66eff765493f3396730b9eef750144d637cda2bd3891a9fbed42461`

| 시험 | 확인 결과 |
|---|---|
| CNPG 합성 로그 → 고정 그룹·Pod UID 스트림 | 실제 exporter가 전송, JSON 본문과 표식 유지 |
| 입력의 목적지 변경 필드와 SQL/임의 민감 표식 | 모두 제거, 등록된 목적지만 요청 |
| PutLogEvents 권한 거부 | fixture 저장 0건 |
| 거부 중 Collector 종료 → 동일 영속 저장으로 재시작 | 허용 복구 후 대기 이벤트 도착 |
| HTTP 200 + 너무 미래인 이벤트의 부분 거부 | 실제 저장에 도착하지 않음; exporter 응답만으로 성공 판정 불가 |
| 정리 | 자체 컨테이너·네트워크 제거, 기존 자원 변경 없음 |

네트워크는 internal이고 공개 포트·실제 AWS 자격·호스트 AWS 설정 mount가 없다.
컨테이너는 비루트·읽기 전용 rootfs·capability 제거·no-new-privileges·자원 상한으로
실행했다. 임시 fake 키는 AWS에 전송하지 않았다. 합성 API 요청 6회, 수신 batch 2개를
기록했다. fixture는 API 형식/분기 시험이며 AWS IAM이나 서명 검증기가 아니다.

등록·사전 확인 단위 시험은 대상/키 불일치, 누락·중복·부분 페이지, 보존 약화,
KMS 비활성·다른 용도, 그룹/스트림 재생성, TLS 해제·커스텀 endpoint, 시간 만료를
거부한다. 기존 OTLP 구성 계약과 감사 검색 회귀도 유지했다.

구현: [구성·사전 확인](../../../services/control-runner/src/bravo_controls/cloudwatch_audit.py),
[재현 시험](../../../services/control-runner/tests/integration/verify_cloudwatch_audit_export.py),
[운영 인수 경계](../../../services/control-runner/docs/source-cloudwatch-audit.md).

관련135개, 전체1,335개와58subtests 통과(기존 조건부5개 제외), Ruff·비밀/공개증빙·출처
검사 통과. 최초 전체 실행은 sandbox의 loopback bind 차단으로 기존11시험이 실패했으며,
허용된 로컬 네트워크 환경에서 동일 코드로 전체 재확인했다. 검사 예외는 추가하지 않았다.
