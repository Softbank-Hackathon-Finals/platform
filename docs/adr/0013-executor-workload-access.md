# [ADR-0013] Executor의 변경 가능한 풀러 설정과 파드 한정 실행 권한

* **상태:** 제안됨(Proposed)
* **날짜:** 2026-11-03
* **관련:** T6 #11, T7 #12, T10 #19, T12 #22, T30 #17

## 배경

T12는 PAUSE한 PgBouncer 안의 설정을 바꾸고 RELOAD한다. ConfigMap 직접 마운트는
kubelet 전파를 기다려야 하고 파일이 읽기 전용이다. 앱 관리 호출은 앱 파드의 토큰을
사용한다. T7 초기 권한에는 exec가 없으며 앱·풀러와 DB가 같은 namespace에 있다.

## 고려한 선택

ConfigMap 직접 마운트는 배포가 간단하지만 즉시 파일 교체 계약을 충족하지 않는다.
namespace 전체 exec는 동적 파드 교체에 편하지만 CNPG 제어 경계를 우회한다.
별도 프록시/전용 namespace는 더 세밀한 통제가 가능하지만 현재 배포 구조 변경이 필요하다.

## 결정 제안

ConfigMap → 동일 UID initContainer → emptyDir를 사용하며 풀러는 단일 replicas와
Recreate로 운영한다. 개발 retry는 2초를 시작값으로 둔다. TLS/Secret 볼륨은 유지한다.
기존 최소권한 Role을 보존하고 정확한 앱·풀러 파드 이름에만 exec를 추가한다.
새 파드의 권한 갱신은 배포 소유자가 수행하며 Executor의 자체 권한 수정은 허용하지 않는다.
DB 제어 역할과 네트워크는 실제 T12 명령/서버 계약을 검토해 별도로 연결한다.

## 결과와 검증 경계

설정은 파드 재생성 시 ConfigMap 값으로 돌아온다. 기록 일치와 실제 backend 대조가
필요하며 불명확한 상태에서는 기존 설계의 펜싱·SAFE_HOLD를 유지한다.
상태 머신·실패 분기·승인 규칙은 바뀌지 않는다. 실제 배포·네트워크·T12 통합 시험 및
팀 리뷰 전에는 채택/완료로 표시하지 않는다.
입력·운영 순서·통합 기준은 [연결 계약](../contracts/T12-platform-integration.md)에 있다.
