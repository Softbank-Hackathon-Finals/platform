# T34 환경 내부 감사 전달 구성

`audit_forwarding.collector_config`는 검증된 등록 정보로 OpenTelemetry Collector Contrib 0.164.0 구성을 만든다. 파일 읽기·체크포인트·영속 전송 큐·TLS는 OTel에 맡긴다. 자체 로그 수집 엔진이나 새로운 중앙 저장소를 만들지 않는다. 설정 생성은 등록 인증·배포·수집 완료 판정이 아니다.

## 경계

- ADR-0010대로 원문 감사 로그는 발생 환경 안에 둔다. `environment`와 `destination_environment`가 다르면 구성 생성을 거부한다. 문자열 일치만으로 실제 위치가 증명되지는 않는다. 운영자는 인증된 endpoint 등록, DNS/경로/저장 위치와 방화벽을 별도로 확인해야 한다.
- CNPG는 등록된 namespace·Pod 이름·Pod UID의 `postgres` 컨테이너 경로만 읽는다. 다른 Pod UID, 앱 로그, 일반 OTLP 입력을 자동으로 수집하지 않는다. DB failover·Pod 교체 후에는 새 UID를 확인해 등록을 갱신한다. 구 UID 수집기만 살아 있다고 감사 수집을 정상 판정하지 않는다.
- 노드 감사 수집은 별도 명시 선택이다. RKE2가 생성하는 감사 파일만 `/var/log/bravo-audit/kubernetes`에 읽기 전용으로 연결한다. EKS control-plane audit는 기존 CloudWatch 수집 경로를 사용하므로 이 옵션을 켜지 않는다.
- namespace·Pod UID·node UID·등록 digest는 앱 JSON이 아니라 등록 객체에서 설정한다. 외부 제출 JSON을 이 자료형으로 변환해 신뢰하는 API를 만들면 안 된다.
- 원본 파일 접근은 노드 로그를 읽는 운영 권한이다. 앱 Pod에 hostPath를 추가하거나 사용자 namespace의 Pod Security를 완화하지 않는다. 전용 수집 실행 단위에 **정확한 로그 디렉터리만** read-only로 연결하고, host network/PID/Docker socket·서비스 계정 자동 토큰·클러스터 쓰기 권한을 주지 않는다. 필요한 호스트 파일 읽기 ACL은 운영자가 별도로 관리한다.

## 전달 데이터와 신원

CNPG 접속 승인 메시지와 pgAudit의 사용자·DB·application_name·DDL 객체/명령을 보존하고, SQL 본문·파라미터·기타 필드는 제거한다. K8s 감사는 동작·주체·객체·결과 코드·시각을 보존하며 요청/응답 본문, URI, 사용자 extra는 제외한다. impersonation 표식은 유지한다. 이는 T34 시험에 필요한 별도 감사 스트림이며 원본의 규정상 보존 정책을 대체하지 않는다.

K8s `user`, `impersonatedUser`, `objectRef`, `responseStatus`가 객체가 아닌 문자열·배열·null이면 해당 필드를 제거한다. 타입이 잘못된 값을 중첩 필드 필터의 예외로 전달하지 않는다. 필수 필드가 제거된 행은 후속 검색·판정에서 유효한 카나리 증거로 받아들이면 안 된다. 실제 Collector 시험은 이런 행 12개와 정상 이벤트를 함께 전달해 민감 표식 제거와 정상 이벤트 도착을 확인한다.

목적지 수신기는 같은 환경의 등록된 OTLP HTTPS endpoint다. CA·서버 이름 검증과 클라이언트 인증서(mTLS)를 사용한다. 인증서는 `/var/run/bravo-audit/tls`의 읽기 전용 Secret으로 제공하고, 로그나 구성에 키 값을 넣지 않는다. 수신기는 인증서 주체를 해당 환경의 쓰기 전용 권한에 연결해야 한다. 검색 역할은 별도다. mTLS만으로 권한 매핑·보존·감사 저장소 불변성이 완료되는 것은 아니다.

이 PR은 운영 수신기나 저장소를 선택·설치하지 않는다. AWS는 CloudWatch, 목적지는 환경 내 저장소에 연결하는 후속 배포가 필요하다. OTLP body는 허용 필드만 남긴 구조화 객체이며 검색 어댑터는 원본/수집 시각과 등록 stream을 보존해 T34 `AuditBatch`로 연결해야 한다. 파일 export 시험을 CloudWatch 검색 성공으로 표시하지 않는다.

## 중단·복구와 한도

- 파일 offset과 exporter queue를 영속 `file_storage`에 저장한다. `fsync=true`, 손상 상태 자동 초기화 `recreate=false`다. 디렉터리가 없으면 시작에 실패하며 자동으로 임의 디스크를 만들지 않는다.
- receiver/exporter 재시도의 만료 시간을 0으로 두어 일시 중단 후 자동 폐기를 막는다. 무한 용량이나 유실 0을 보장하지 않는다. 각 storage DB 64 MiB, queue 2,048 요청, 동시 파일 16개, 로그 1 MiB, memory limiter 96 MiB/24 MiB로 제한한다. offset/queue DB가 별개이므로 총 디스크 상한은 별도 볼륨 quota로 제한한다.
- batch processor를 두지 않아 전송 전 메모리 배치에만 남는 구간을 줄인다. 정확히 한 번 전달을 주장하지 않는다. 복구 시 중복은 원본 이벤트 식별자로 처리하고, 중복·미완전 페이지를 판정기가 정상으로 덮어쓰지 않아야 한다.
- 파싱/변환 실패는 `propagate`다. 잘못된 입력이나 큰 로그를 성공한 감사 이벤트로 바꾸지 않는다. poison record·회전·보존 시간 초과·디스크 고갈로 수집이 막힐 수 있으며 운영 알림과 수동 복구가 필요하다. 원본을 임의 삭제해 넘기지 않는다.
- 수집기 자체 metric endpoint는 loopback이다. 각 환경 운영자가 receiver refused/exporter failed/queue occupancy/storage 오류·디스크 사용량·최근 실제 canary 도착을 관측해야 한다. 이 PR은 경보 배달 연결을 완료하지 않는다. 프로세스 liveness만으로 AUD-01 PASS를 내리지 않는다.
- TLS 실패·수집 중단의 진단 로그는 환경 내부 운영자만 읽는다. OTel 오류에 레코드 일부가 포함될 수 있으므로 표준 출력 전체를 플랫폼·PR·공개 증빙으로 전달하지 않는다.

## 독립 재현

고정 이미지는 `otel/opentelemetry-collector-contrib@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b`다. 패키지의 잠금 환경을 사용한다.

```sh
PYTHONPATH=src python tests/integration/verify_audit_collector.py --private-output "$PRIVATE_OUTPUT/new-run"
```

시험은 생성한 Docker 내부 네트워크만 사용하고 호스트 포트를 공개하지 않는다. 임시 CA·인증서와 합성 감사 자료를 만들며 원본/출력/진단 파일은 지정한 비공개 경로에 보존한다. 이번 시험이 생성한 컨테이너와 네트워크만 종료한다. CI 기본 단위 시험과 구분한 opt-in 실제 도구 시험이다. 파일 수신기는 시험용이며 운영 감사 검색·변조 방지 저장소가 아니다.

## 실제 환경 완료 조건

1. T6/T30의 인증된 Pod/node UID·로그 경로·환경 내 수신기를 확인한다.
2. 읽기 전용 원본 mount, 영속 checkpoint, 수신기 인증·쓰기/검색 권한·보존·디스크/수집 중단 경보를 연결한다.
3. 실제 로그인·DDL·RBAC canary를 발생시키고 T14가 10분 내 검색·출처·시간·행동을 검증한다.
4. 검색 허용 → 다른 역할 거부 → 허용 재확인, 수집 중단·복구·지연·누락·권한 회수를 시험한다.
5. 양쪽 실측 결과가 있기 전에는 T34 전체 완료로 처리하지 않는다.

## 고정 버전 공식 근거

- [OTel filelog 0.164.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.164.0/receiver/filelogreceiver/README.md): offset, 회전, 재시도와 전달 유실 한계
- [File storage 0.164.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.164.0/extension/storage/filestorage/README.md): fsync, 손상 복구·크기 제한
- [File exporter 0.164.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.164.0/exporter/fileexporter/README.md): 시험 수신기의 OTLP JSON 저장 형식

## AWS 출발지 직접 전달

출발지 CNPG → CloudWatch는 [별도 등록 구성](source-cloudwatch-audit.md)을 사용한다.
기존 OTLP 호출 계약은 유지하며, 파일 수집·필드 제한·출처 설정을 공통으로 사용한다.
CloudWatch 구성은 운영 적용/전달 완료가 아니며 실제 카나리 검색이 필요하다.
