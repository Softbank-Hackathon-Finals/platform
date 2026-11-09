# T14 등록 listener v2 실제 출발지 시험

**다른 namespace의 시험 Pod에서 출발지 DB와 PgBouncer의 TCP 연결이 모두 성공했다.**
두 역할 모두 전후 허용 연결·시험 서비스·DNS 대조가 성공하여, 후보 방법 v2의
NET-01.3은 **FAIL(`observed: allow`)**이다. 나머지 19개는 실행하지 않아 UNKNOWN이다.
SQL·비밀번호·업무 payload는 0바이트이며, DB 로그인 성공이나 데이터 유출을 뜻하지 않는다.

## 기존 시험과 달라진 점

[고정 포트 v1 시험](https://github.com/Softbank-Hackathon-Finals/platform/pull/432)은 풀러 6432의
정상 대조 실패로 UNKNOWN이었다. 실제 풀러는 5432다. v1 결과를 수정하지 않고
[등록 listener v2](../../../services/control-runner/docs/registered-network.md)를 별도로 실행했다.
기본 T5 카탈로그와 운영 T19 게이트는 v1이며, 이번 결과를 그 계획에 넣지 않았다.

| 구분 | 실제 검증 |
|---|---|
| DB 역할 | DB Service UID → EndpointSlice → Ready CNPG Pod·Cluster 소유 관계 |
| 풀러 역할 | 풀러 Service UID → EndpointSlice → Ready Pod → ReplicaSet → 등록 Deployment |
| listener | 서로 다른 서비스 IP의 5432. 두 서비스를 별도로 연결 |
| 등록 대조 | 시험 전·첫 역할 후·두 번째 역할 후 서비스·backend 신원/설정 동일 |
| 정책 대조 | source/test namespace 정책, 노드 보안 그룹 원문 해시 전후 대조 |
| 앱 보호 | 기존 앱 UID·spec·이미지·컨테이너 상태 전후 대조, 업무 Service 라벨을 시험 Pod에 붙이지 않음 |

source namespace에 NetworkPolicy·SecurityGroupPolicy가 없는 것도 관측했다.
이 설정만으로 FAIL을 추론한 것이 아니라 실제 TCP 연결을 확인했다. 동일 노드에서
실행했으므로 모든 노드나 외부 인터넷 경로까지 검증한 것은 아니다.

## 시간 검증의 발견과 수정

첫 두 실행은 원격 에이전트 시각이 수집기 수신보다 약 0.401초 앞서 UNKNOWN이었다.
추가 계측에서 요청·응답은 19.537ms였고 원격 관측 시각은 수신 시각보다 400.923ms
앞섰다. 이를 네트워크 단절이나 정책 거부로 세지 않았다. 두 실행의 자원도 각각 정리했다.

v2에서 인증된 요청·응답 구간과 nonce·agent·endpoint를 신선도 근거로 사용하도록
수집 계약을 명시했다. 원격 시각은 그대로 남기고, 수집기 수신 시각을 증거 시각으로 쓴다.
원격 시각 차이는 최대 1초이며 초과하면 거부한다. 기존 v1 시간 조건은 유지한다.
운영 게이트 활성화에는 이 후보 방법과 시간 조건의 별도 인수가 필요하다.

최종 실제 실행은 [result.json](result.json)의 별도 run_id와 해시로 구분한다.
일부 실패 실행의 timestamp를 새 시각으로 바꾸거나 결과를 합치지 않았다.

## 검증과 정리

- 관련 회귀 101개, 전체 T14 1,384개와 subtests 58개 통과. 기존 조건부 5개 제외.
- 작은 시계 차이 보존·초과 차이 거부, 구·신규 방법 오용 차단, 역할 누락/중복/대상 재사용,
  등록 변경, 대조군 실패, 공통 시간 예산, 저장 실패를 검사했다.
- 시험 Pod 3개와 namespace 1개를 UID/resourceVersion 조건으로 삭제하고 부재 확인.
  비루트·restricted PSA·서비스 계정 토큰 없음·capability 제거·읽기 전용 root·자원 한도를 적용했다.
- 공유 SG·NetworkPolicy·DB·앱 설정은 변경하지 않았다. 시험은 TCP 연결만 수행했다.

필요한 후속은 T6의 실제 허용 흐름을 보존하는 출발지 접근 정책 검토, 목적지의 같은 방법
시험, T5/T19의 v2 두 역할 증거 검증과 T10 새 계획 결속이다. source FAIL을 보안 개선 후
target PASS와 비교할 수 있는지는 등록된 게이트 규칙에 따라 판단한다. 현재 T14 완료나
실제 양쪽 PASS로 기록하지 않는다.
