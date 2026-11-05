# [ADR-0020] 플랫폼 감사 로그: 실행별 해시 체인을 S3 Object Lock에 한 기록 한 객체로

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-05
* **관련 (Related):** T24 #28, T10 #19, T11 #20, T21 #27. 설계 7.4, 8.3, 8.4, FR-14. [ADR-0016](0016-target-audit-store.md)(목적지 감사 저장소), [ADR-0019](0019-agent-tool-boundary.md)(도구 이벤트 열거값), [T24 기록 계약](../contracts/T24-audit-record-v1-proposal.md)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 설계는 "모든 단계 · 판정 · 차단 · 승인을 실행 ID별로 S3 Object Lock에 기록"한다고 적었다. T10은 이벤트를 SQLite · PostgreSQL에 저장하지만 README에 "Object Lock 불변 저장소가 아니다, T24는 별도"라고 명시했다. T11 도구 이벤트도 시도 결과 안에만 남는다. ADR-0016은 **목적지**(RKE2)의 앱 · DB · K8s 감사 로그를 SeaweedFS Object Lock에 두기로 했고, **플랫폼** 자체의 결정 기록 저장소는 비어 있었다.
* **문제:** (1) 기록 형식이 없으면 콘솔 · 검토자가 실행 하나의 전체 이력을 순서대로 복원할 수 없다. (2) 저장소가 "지울 수 없다"는 것을 코드와 증빙으로 보여야 한다. (3) 비밀이 섞여 들어가면 지울 수 없는 저장소가 오히려 위험이 된다.

## 2. 고려한 옵션들 (Considered Options)

1. **실행별 해시 체인 + 한 기록 한 객체 + COMPLIANCE 보존 (채택)**: 순서 · 누락 · 변조를 체인으로 잡고, 보존은 저장소가 맡는다. 기록 단위가 작아 영수증을 콘솔에 바로 붙인다.
2. 실행 종료 시 JSON Lines 한 파일로 업로드: 실행 중 사고가 나면 기록이 없다. 기각.
3. T10 PostgreSQL에 append-only 테이블: DB 관리자가 지울 수 있고 설계가 요구한 Object Lock이 아니다. 기각.
4. 목적지 SeaweedFS(ADR-0016)에 플랫폼 기록도 저장: 플랫폼 → 목적지 방향 쓰기 경로가 생기고 목적지 장애가 플랫폼 감사에 영향. 기각. 다만 identity 분리 모델은 그대로 따른다.

## 3. 결정 (Decision)

* 기록 v1: `record_id = run_id:sequence`, `prev_sha256` 연결, `sha256`은 나머지 전체. `payload`는 원 이벤트 그대로이되 비밀 패턴이 있으면 기록을 거부한다.
* 저장: `audit/<plan>/<run>/<seq>.json`, `If-None-Match: *`, `ObjectLockMode=COMPLIANCE`, 보존 7일(데모). 응답 `VersionId`와 readback이 없으면 기록 성공으로 보고하지 않는다.
* 증빙: 삭제 · 버전 삭제 · 보존 단축 · 덮어쓰기 · 버킷 정책 · Lock 설정 변경 여섯 시도가 모두 거부돼야 PASS. 결과 코드만 기록한다.
* 데모 8.4 장면은 `kind=DESTRUCTIVE_ATTEMPT_BLOCKED, category=bucket_delete` 기록으로 식별한다(ADR-0019 열거값).
* 플랫폼은 AWS S3(설계 7.4), 목적지는 ADR-0016의 SeaweedFS. 권한 모델(쓰기 전용 · 검색 전용 · 관리 분리)은 같다.

## 4. 결과 (Consequences)

* 좋은 점: 실행 하나의 이력을 체인 하나로 검증한다. T10 · T11이 바뀌지 않아도 붙는다. 콘솔은 영수증 · chain_digest만 표시하면 된다.
* 나쁜 점: 해시 체인은 writer가 정직하다는 전제의 순서 보장이다. writer가 처음부터 다른 체인을 쓰면 체인만으로는 모른다. 저장소 보존(Object Lock)과 쓰기 역할 분리가 그 부분을 맡는다. Object Lock은 S3 API 계층 보호라 보존 만료 뒤 삭제와 계정 루트의 버킷 삭제는 막지 못한다.
* 미검증: 실제 버킷 · 역할 · readback · 거부 탐침. T10 writer 연결 위치.

## 5. 실측 (Evidence)

로컬 `services/audit-log` 시험 25개(체인 결정성 · 변조 6종 검출 · 비밀 5종 거부 · resume · 싱크 파라미터 · 미보존 쓰기 거부 · 거부 탐침 PASS/FAIL). 실제 AWS 실측은 없다.


## 6. 개정 (2026-11-06): 실제 버킷 실측과 데모 보존 1일

* **보존 기간:** 데모 기본값을 7일에서 **1일**로 바꿨다. COMPLIANCE 보존 중에는 계정 관리자도 객체와 버킷을 지우지 못하므로, 데모 다음 날 플랫폼 계정을 정리할 수 있게 하려는 것이다. 운영은 규제가 정한 기간을 명시적으로 준다. 사용자(T24 담당) 결정.
* **인프라:** `infra/envs/platform-audit`이 버킷 · 전용 KMS 키 · 쓰기 역할 · 검색 역할을 소유한다. 버킷 정책은 권한을 주지 않고 빼기만 한다. 기록 삭제와 보호 설정 변경은 관리자 역할 외 전원 거부, 거버넌스 우회는 전원 거부, 평문 HTTP 거부다.
* **쓰기 역할의 목록 조회:** 같은 실행의 체인을 저장소에 실제로 남은 기록에서 이어 쓰려면 목록 조회가 필요해 `audit/` 접두사 한정 `s3:ListBucket`을 쓰기 역할에 추가했다. 삭제 · 보호 변경 권한은 여전히 없다.
* **실측:** 실제 버킷에 T10 `PlanStore` · T11 도구 계층이 만든 이벤트 4건을 쓰고, 재전송 3건 흡수, 검색 역할로 체인 검증, 쓰기 역할의 삭제 거부 탐침 6종 PASS와 `COMPLIANCE_ACTIVE`를 확인했다. 관리자 역할로도 잠긴 버전 삭제 · 보존 단축이 거부됐다. [증빙](../evidence/t24-audit-live-2026-11-06/README.md).
* **남은 것:** T10 서비스의 상시 기록 경로, T10 런타임 역할을 쓰기 신뢰 주체로 추가, `data.tool_events` 배열 분해.
