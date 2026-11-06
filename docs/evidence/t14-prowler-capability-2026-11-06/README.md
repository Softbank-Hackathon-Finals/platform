# T14: AWS 조회 전 Prowler 분석기 자격

기존 [실환경 자격 실패](../t14-prowler-2026-11-06/README.md)는 Prowler가 분석 오류
뒤에도 PASS 행을 반환한 사례다. 검사 실패를 지우거나 패턴을 빼서 통과시키지 않고,
실제 계정을 조회하기 전에 도구 환경의 분석 가능 여부부터 확인하도록 보강했다.

## 확인한 결과

- 설치된 공식 Prowler 5.47.0·py-iam-expand 0.3.0·IAM 데이터 패키지로 사전 검사했다.
  분석기 파일은 공식 태그의 바이트와 일치했다.
- 현재 전체 패턴 집합은 고유 126개다. 정렬한 첫 패턴 확장부터 불가하여
  `ready: false / ANALYZER_CATALOGUE_INCOMPATIBLE`로 끝났다. 126개 모두를
  성공적으로 검사했다는 의미가 아니다. 패턴과 오류 원문 대신 지문을 남긴다.
- 실제 Python audit hook을 켠 별도 검사에서 네트워크 시도와 AWS 자격 파일 읽기는
  모두 0이었다. 이는 해당 실행의 관측이며 네트워크 샌드박스의 격리 보장이 아니다.
- 실제 실행 도구에 없는 프로필과 없는 등록 파일을 전달해도 먼저 사전 검사에서
  UNKNOWN으로 중단됐다. AWS provider·정책 수집은 시작하지 않았다.
- 새 회귀 26개, 모든 opt-in을 켠 전체 1,545개와 subtest 58개 통과, skip 0.
  고정 Gitleaks 실제 실행과 고유 로컬 PostgreSQL/PgBouncer TLS 시험을 포함한다.
- 확장 오류 무시·빈 확장 허용·소스 지문 생략·worker 사전 검사 생략의 네 변이를
  각각 7/1/2/4개 회귀가 검출했다. 실제 변이 import 경로와 지문을 확인했다.

기본 전체 실행의 조건부 skip 5개는 고정 Gitleaks·Docker TLS를 켜 해소했다.
추가한 재검사·심볼릭 링크 거부 두 회귀까지 최종 전체를 확인했다.
패키지 목록 보존의 첫 시도는 기본 캐시 디렉터리 쓰기 제한으로 실패했으며,
기존 허용 캐시를 명시해 해결했다. 정책·검사 예외·시험 조건은 낮추지 않았다.

## 도구를 교체한 것은 아님

조사 중 공식 [5.48.0](https://github.com/prowler-cloud/prowler/releases/tag/5.48.0)이
공개된 것을 확인했다. 관련 분석기 파일은 5.47.0과 동일했다.
5.48.0의 전체 의존성·런타임을 설치하거나 자격 검증한 것은 아니므로 그 버전이
같은 결과를 낸다고 단정하지 않는다. 자동 업그레이드·새 버전 수용은 하지 않았다.

공식 소스:
[5.47.0 분석기](https://github.com/prowler-cloud/prowler/blob/5.47.0/prowler/providers/aws/services/iam/lib/privilege_escalation.py),
[5.48.0 분석기](https://github.com/prowler-cloud/prowler/blob/5.48.0/prowler/providers/aws/services/iam/lib/privilege_escalation.py).

## 운영 인수와 구분

`ready: true`도 기존 실제 정책 수집을 시도할 수 있다는 의미다. IAM-01.2 PASS,
승인된 매핑, Kubernetes RBAC 대조, 최소 권한 또는 금융 규제 적합성의 판정이 아니다.
이번 실행은 AWS 통제 시험을 수행하지 않았고 기존 UNKNOWN을 유지한다.
운영 역할·정책·앱·DB·기준선·게이트 권한은 변경하지 않았다.

[summary.json](summary.json)에 실행 범위와 지문을,
[source-manifest.json](source-manifest.json)에 구현·시험 소스를 고정했다.
원시 도구 결과·변이·설치 환경 목록은 비공개로 보존했다.
