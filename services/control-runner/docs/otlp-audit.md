# 환경 내부 OTLP 감사 기록을 T14 관찰로 연결

`otlp_audit.decode_export`는 인증된 환경 내부 수신기/저장소가 보존한 OTLP JSON을
기존 `AuditSearchProbe`가 읽는 `AuditEvent`로 변환한다. 외부 업로드 API, 인증 서버,
감사 저장소 자체를 구현한 것은 아니다. 원문 감사 로그는 ADR-0010에 따라 환경 안에 둔다.

## 신뢰 경계

- `AuditOrigin`은 수신기가 인증한 Collector 등록에서 결정한다. payload의 attribute를
  읽어 등록을 새로 만들거나 사용자가 보낸 origin을 신뢰하면 안 된다.
- 환경·등록 digest·노드 UID·Pod UID·namespace·컨테이너를 정확히 대조한다. 다른 Pod,
  다른 환경, 미등록 Kubernetes 노드 원천은 전체 export를 거부한다.
- `received_at`은 **저장소가 기록 보존을 확인한 시각**이다. 송신기의
  `observedTimeUnixNano`를 이 값으로 쓰지 않는다. 수집 기한을 지난 기록은 기존 시험기가
  검색 성공으로 소급 인정하지 않는다.
- CNPG는 CRI의 `timeUnixNano`, Kubernetes는 API 서버의 `stageTimestamp`를 사건 시각으로
  사용한다. 시계 동기화가 안 돼 수신 시각보다 미래이면 결과를 수용하지 않는다.
- `receipt_id`는 수신 저장소가 발급한 불변 식별자다. 보존 export의 내용 해시와 인증된
  생산자·수신 시간도 저장소가 결속해야 한다. 디코더가 서명이나 저장 무결성을 제공하지 않는다.

## 제한과 실패 처리

export는 최대 1 MiB·1,000건, resource/scope 수와 중첩 깊이를 제한한다. 중복 JSON 키,
중복 OTLP key, 서로 다른 AnyValue를 함께 넣은 값, metadata drop, 미정제 attribute,
잘못된 시각은 거부한다. 한 기록만 잘못돼도 앞부분을 성공으로 반환하지 않는다.

등록된 감사 필드의 문자열·정수·불리언·map을 처리한다. Collector `ParseJSON`이 HTTP 상태
코드를 doubleValue로 보내므로, 정확히 표현 가능한 정수만 변환한다. 소수·정확 정수 범위
초과·배열·bytes 등 지원하지 않는 값은 거부한다. 범용 OTLP 서버 구현을 주장하지 않는다.

이벤트 ID는 보존 export receipt와 행 위치에 결속된다. 같은 export 재조회에는 같은 ID를
사용한다. 별도 재전송이 중복 저장될 수 있으므로 exactly-once 수집을 주장하지 않는다.

## 검색 어댑터의 책임

디코더 이후에도 저장소의 인증된 검색 API가 필요하다. 서버가 선택한 원천·시간 창·marker를
검색하고 모든 페이지를 읽어야 한다. 권한 거부·시간 초과·잘린 페이지·손상 export는 UNKNOWN이며,
일부 matching event가 있다고 `complete=True`로 만들 수 없다. 기존 `AuditSearchProbe`가
행동·사용자·DB·namespace·marker·기한을 다시 검사한다. 검색자는 저장·삭제 권한이 없어야 한다.

## 실제 연결 시험

`tests/integration/verify_otlp_audit_search.py`는 [T34 전달 설정 PR #117](https://github.com/Softbank-Hackathon-Finals/platform/pull/117)의
검증 harness를 명시적으로 받아, 고정 digest Collector의 mTLS 전달·정제·디코딩·AUD-01.1/2/3
의미 검사를 연결한다. 일회용 Docker 내부망만 사용한다. 기본 입력·canary receipt는 합성이며
파일 exporter는 시험 저장소다. 이 사실과 실제 DB/API 기록 캡처 시험을 증빙에서 구분한다.

```sh
PYTHONPATH=src python tests/integration/verify_otlp_audit_search.py \
  --forwarding-harness <검토한_T34_체크아웃>/tests/integration/verify_audit_collector.py \
  --private-output <새_비공개_결과_경로>
```

[실행 증빙](../../../docs/evidence/t34-otlp-audit-search-2026-11-03/README.md).
운영 배포, 보존·검색 백엔드, 로그 유실 경보, 팀 EKS/RKE2의 수신 권한 검증은 남아 있다.

## 표준 근거

- [OTel Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/): 사건 시각과 관측 시각 구분.
- [OTLP](https://opentelemetry.io/docs/specs/otlp/): 전송 형식. 이 구현은 등록된 감사 자료의 제한된 소비자다.
