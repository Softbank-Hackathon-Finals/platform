# T21 보안·IaC·고객 동기화 UI 개발 기록 · 2026-11-07

## 기준과 변경 이유

- 시작 정본: `develop`의 `1b3bd49d`.
- 보존한 Sova UI·워크로드 모션: 기존 `feature/T21-workload-response-motion`의 `3cd07ad4`. 새 화면으로 교체하지 않고 해당 기능 이력을 병합했다.
- 병합 기준: 최신 T24 `RegisteredAuditRun`·버전 있는 읽기·등록값 기준점을 유지했다. 옛 `RegisteredAuditSnapshot`을 복구하지 않았고, 합성 확장 시험 fixture도 실제 T24 FileSink 영수증으로 수정했다.
- 변경 코드: 보안 요약 `cc4eaa6a`, delivery 읽기·호스트 포트 `f8ecbb34`, IaC·고객 UI `325ad51f`.
- 실제 native API 대조: T44 API [#628](https://github.com/Softbank-Hackathon-Finals/platform/pull/628)의 `51482e6f37faec8e436efbd6e4aff87b670ee8a5`. 검증 시 해당 checkout은 수정되지 않은 상태였다.
- 고객 재시도 생산자: [#677](https://github.com/Softbank-Hackathon-Finals/platform/pull/677). 미병합 의존성과 UI에서 받을 수 있는 상태를 구분한다.

문서 커밋·UI 코드 커밋·수집기 버전·검토한 고객 desired-state SHA·실제 배포 이미지 원본은 서로 대체하지 않는다. 이 작업에서는 배포 이미지를 만들거나 공용 환경을 변경하지 않았다.

## 구현

1. 같은 계획의 T19 보고서를 보안 화면과 워크로드 지도에 표시한다. 짧은 조회 권한·계획 버전·digest가 맞지 않으면 비교 상태를 철회한다. FAIL을 공격이나 실제 격리의 증거로 바꾸지 않는다.
2. 기존 T44 계획 API와 T10 infra 승인을 사용하는 IaC UI를 추가했다. 요청자 자기 승인 차단, 고정 binding, 승인 조회, PR 영수증, 응답 유실 후 같은 요청 복구를 구분한다. 다른 운영 요청과 미확인 IaC 요청의 동시 제출을 막는다.
3. 등록된 고객 클러스터·SHA·생산자 버전에 결속한 읽기 포트를 추가했다. 기존 GET 전용 수집기의 결과를 소비하고, 실환경·합성·과거 관측을 화면에 구분한다. 고객 수집이 없으면 빈 성공을 만들지 않는다.
4. 기존 Sova 탐색에 `IaC·고객 환경` 메뉴를 추가했다. 데스크톱과 모바일에서 원래 화면 구성·색·브랜딩을 유지했다.

## 검증

| 검증 | 결과·범위 |
|---|---|
| Sova·최신 T24 통합 기준 | Python 380개, JS 272개 통과 |
| 최종 console Python | **413개 통과, 1개 의존성 skip**. native arm64 PostgreSQL 17의 임시 로컬 DB 포함 |
| native T44/T10 상호운용 | **1개 통과**. #628 고정 코드의 실제 승인 저장·OperatorAccess·claim 사용. 사람 신원·Git 저장소는 명시적 fixture이며 실제 GitHub 변경/고객 적용 아님 |
| JS 상태·계약·기하·응답 유실 | **280개 통과** |
| 실제 브라우저 회귀 | **58개 통과**. IaC HTTP 응답은 계약 fixture, T10 HTTP/SSE·계획 경계는 로컬 실제 서비스 |
| Python Ruff / JS lint / 정적 자산 build | 통과 |
| 공개 텍스트 검사 / gitleaks | 통과. 자동 검사에서 제외된 기존 아틀라스 JPG 2개도 수동 확인: 설계 그림이며 자격·계정/운영 리소스 ID 없음 |

Python 전체 시험 준비 중 `pg_config`가 없는 PATH에서 DB fixture 초기화가 실패했다. 시험을 제외하지 않고 기존 native arm64 PostgreSQL 도구를 PATH에 지정해 전체를 재실행했다. 공유 DB는 사용하지 않았다.

로컬 원본 로그·데스크톱/모바일 화면은 비공개 검증 디렉터리에 보관했다. 공개 문서에 장치 홈 경로·개인 식별자·운영 자원 주소를 복사하지 않았다. 재현 명령과 운영 연결 조건은 [소비·호스트 연결](security-delivery-ui.md)을 따른다.

## 운영 미완료

T21 소비 코드와 화면 검증을 완료한 상태다. T21 전체 시연 완료나 실제 공격 대응·환경 이전 완료로 체크하지 않는다.

- #628의 운영 API, 호스트 권한·세션 주입, 실제 GitHub App 연결은 배포·인수가 필요하다. 의존성 병합 시 관련 path dependency lockfile을 함께 다시 해석한다.
- 실제 고객 수집기와 인증한 등록 저장소, 목적지 IAM-01.1의 후속 조치·재시험, 실제 워크로드 대응 증거는 소유자 연결·인수가 필요하다.
- 로컬 시나리오 모션, 실제 격리, 고객 GitOps 동기화, 데이터·서비스 이전의 성공 범위는 끝까지 분리한다.
