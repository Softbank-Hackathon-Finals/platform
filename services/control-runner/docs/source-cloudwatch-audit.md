# T34 출발지 CNPG 감사 → CloudWatch

`cloudwatch_audit`는 기존 OTel 파일 수집·필드 제한·영속 큐를 사용해 **등록된 CNPG Pod의
감사 로그만 같은 AWS 계정·리전의 지정 스트림에 전달하는 구성**을 만든다. 구성 생성과
읽기 전용 사전 확인은 배포·운영 수집 성공·AUD-01 PASS를 의미하지 않는다.

## 등록과 실행 경계

- `CloudWatchAuditRegistration`은 인증된 운영 등록에서 생성한다. 환경, namespace,
  Pod 이름/UID, node UID, 등록 digest와 정확한 로그 그룹 ARN·KMS 키 ARN·보존 기간을 묶는다.
  출발지와 저장 환경이 다르거나 계정/리전이 다른 KMS 키, wildcard·템플릿은 거부한다.
- 스트림은 `cnpg/<namespace>/<pod>/<pod-uid>/postgres`로 고정한다. Pod 교체 후에는
  새 UID의 등록·스트림·정책 검토가 필요하다. 파일 접근/노드 소유권 검증은 기존
  [감사 전달 경계](audit-forwarding.md)를 그대로 따른다.
- `destination_preflight`는 그룹·스트림·KMS를 두 번 읽는다. 정확한 ARN, KMS 활성/용도,
  보존 기간, STANDARD 그룹, 생성 시각을 대조한다. 페이지가 남거나 자원이 누락·교체되면
  실패한다. 호출 전후 60초 예산을 검사하며 각 SDK 호출도 제한한다. 호출 도중 중단을
  강제하는 프로세스 제한은 호스트가 담당한다.
- 고정 AWS HTTPS endpoint와 인증서 검증을 확인한다. 커스텀 endpoint와 TLS 검증 해제는
  거부한다. 사전 확인 반환값은 `configuration_ready=true`, `delivery_verified=false`,
  `gate_eligible=false`다. 키 정책·전용 writer의 실제 접근·디스크 여유·운영 신원을
  이 반환값 하나로 증명하지 않는다.

## 자원과 최소 권한

그룹·KMS·보존과 최초 스트림은 운영 IaC가 준비한다. 이 모듈은 생성·갱신 API를 호출하지
않고 자격증명도 렌더링하지 않는다. Collector에는 전용 workload identity를 제공하며
개인 SSO, 노드 전체 역할 또는 장기 키를 넣지 않는다. 읽기 전용 사전 확인/검색 역할은
전송 역할과 분리한다.

`writer_policy`는 **정확한 한 스트림**의 `logs:CreateLogStream`·`logs:PutLogEvents`만
허용하는 정책 제안이다. 실제 설치하거나 유효 권한 전체를 증명하지 않는다. 다른 정책과
경계를 함께 확인해야 한다. 그룹 생성·삭제·보존 변경·KMS 관리·검색 권한은 제안하지 않는다.

고정 OTel 0.164.0은 이미 존재하는 스트림에도 최초 `CreateLogStream`을 요청한다.
따라서 전송만 허용하면 시작이 막힐 수 있다. 이 한 작업은 정확한 스트림 ARN에만
허용하고 그룹 자동 생성 권한은 주지 않는다. 그룹이 없으면 오류로 남아야 한다.
등록 이후 그룹 삭제/재생성 등은 반복 사전 확인과 운영 경보로 감지한다.

## 데이터와 전송 판정

CNPG allowlist 이후의 구조화 본문만 `raw_log`로 전송한다. SQL·파라미터와 임의 필드를
제거한다. `_aws`, `log_group_name`, `log_stream_name` 등 입력 기반 목적지 변경 필드도
제거한다. 일반 앱 OTLP 입력이나 원문 JSON을 이 exporter에 직접 넣지 않는다.
기존 `CloudWatchAuditSearch`가 읽는 CNPG 본문 형태를 유지한다.

file offset과 queue는 기존 영속 저장소를 공유한다. 전송 재시도는 시간 만료로 자동
폐기하지 않지만 queue/디스크는 유한하다. 수집 거부, 포화, 파싱/크기 제한, 원본 회전,
TLS/권한 오류를 운영 경보에 연결해야 한다. **정확히 한 번 전달이나 유실 0을 보장하지 않는다.**

고정 exporter는 alpha이며 다음 제한을 실제 바이너리 시험에서 확인했다.

- HTTP 200 안에 `rejectedLogEventsInfo`가 있어도 전송 호출이 완료될 수 있다.
- 과대 이벤트는 exporter에서 잘릴 수 있다. 잘린 JSON은 감사 증거로 쓰지 않는다.
- 리소스 원문이 아닌 별도 등록/스트림과 실제 검색 결과로 출처를 확인해야 한다.

따라서 **queue 소진/전송 성공 로그만으로 AUD-01을 통과시키지 않는다.** 실제 로그인·DDL
카나리가 지정 스트림에 도착하고, 기존 시험기가 출처·행동·시간을 대조해야 한다.
누락은 대기/FAIL, 조회·전달의 불확실성은 UNKNOWN으로 남긴다.

## 검증과 운영 인수

[고정 Collector 시험](../../../docs/evidence/t34-cloudwatch-export-2026-11-06/README.md)은
합성 CloudWatch 프로토콜 서버와 실제 Collector를 연결한다. 실제 AWS 서비스나 운영
CNPG를 검증한 결과가 아니다.

```sh
PYTHONPATH=src python tests/integration/verify_cloudwatch_audit_export.py \
  --private-output "$PRIVATE_OUTPUT/new-run" \
  --python-image 'sha256:<로컬 Python 시험 이미지 ID>'
```

운영 적용 전 T6와 로그 디렉터리 읽기·전용 역할·그룹/KMS/보존·영속 볼륨을 인계한다.
그다음 관리자 로그인·DDL의 새 카나리, 허용 검색 → 다른 역할 거부 → 허용 재확인,
수집 중단·복구를 실측한다. 목적지 수집과 양쪽 계획 결속은 별도 인수다.

고정 버전 근거: [exporter 설명](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.164.0/exporter/awscloudwatchlogsexporter/README.md),
[본문/라우팅 처리](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.164.0/exporter/awscloudwatchlogsexporter/exporter.go),
[그룹/스트림 생성·부분 거부 처리](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.164.0/internal/aws/cwlogs/cwlog_client.go).
