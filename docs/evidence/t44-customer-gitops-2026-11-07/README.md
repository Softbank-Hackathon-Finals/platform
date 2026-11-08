# T44 고객 GitOps 설치·동기화 분리 검증

고객 클러스터마다 기존 T44 Argo CD Core를 두는 구조를 유지하고, 차트 0.2.0에서
컨트롤러 설치·Application 등록·자동 동기화·자동 삭제를 분리했다.
**로컬 GitOps 수렴은 확인했으며, 팀 공용 고객 클러스터의 설치·인수는 미완료다.**

## 기준을 구분한다

- 시험한 차트·조회 도구: `d19f77c0d519781b673b1bdd2f6bfcac9bdd02dc`.
- 로컬 Argo가 Git에서 읽어 적용한 생성 차트 원본: `7560c2d913259c54712c9a3d28a858ef5e5b97c5`.
- 새 차트 0.2.0은 로컬 Helm으로 설치했다. 위 두 커밋을 같은 배포 원본으로 합치지 않는다.
- 로컬 입력은 arm64 `kindest/node:v1.34.0`, Argo CD `v3.6.1`, 잠긴 argo-helm `10.14.0`이다.
  Tailscale 차트의 고정 해시를 확인하고 CRD만 설치했다. Tailscale operator·실제 tailnet 경로는 시험하지 않았다.

[summary.json](summary.json)은 공개 요약이며 [source-manifest.json](source-manifest.json)은 시험한 코드의 경로·해시다.
대상 UID·kubeconfig·원문과 실패 로그는 비공개 원본에 보존했다.

## 격리된 로컬 실클러스터

2026-11-07 16:39:55–16:41:47 KST에 새 kind 클러스터 하나로 확인했다.

| 단계 | 실제 확인 |
| --- | --- |
| bootstrap | Argo 컨트롤러가 준비됐지만 Application은 0개. 업무 namespace도 생성되지 않음 |
| 정지 등록 | Application을 만들고 autoSync=false 유지. 업무 namespace 미생성 |
| 명시적 활성화 | autoSync=true, prune=false. Git의 고정 커밋에서 생성 차트를 읽어 15개 자원 Synced |
| 조회 인수 | 동일 클러스터·커밋·Helm 입력·최종 sync·최근 조정·컨트롤러 준비를 함께 확인해 SYNC_VERIFIED |
| 잘못된 대상 | 다른 클러스터 UID 또는 다른 커밋을 기대하면 BLOCKED·종료 코드 3 |
| 정리 | 시험에 직접 만든 kind만 삭제하고 목록에서 부재 확인. 기존 로컬 클러스터는 유지 |

Argo의 Healthy·Synced는 실제 업무 통신·Tailscale 경로·DB 무결성이나 무중단의 근거가 아니다.
이 결과는 `authorizes_cutover`, `migration_verified`, `control_equivalence_verified`를 모두 false로 둔다.
외부 저장소 장애·실제 고객의 중복 제어기·네트워크 단절 복구까지 시험한 것으로 확대하지 않는다.

앞선 두 시도의 실패도 보존했다. 첫 시도는 kind의 전체 플랫폼 이미지 반입 중 누락된 manifest로 실패했고,
두 번째는 검증 하네스가 고정 아카이브 파일명을 잘못 가정해 실패했다. 각 클러스터는 정리했다.
최종 실행은 노드의 네이티브 pull과 기존 원문의 고정 archive 해시 대조를 사용했다.
Tailscale CRD는 이 차트의 실제 Helm 렌더에서 추출했다.

## 실제 온프렘의 읽기 전용 확인

- 16:36:05 KST: 등록된 클러스터 UID 일치. Application CRD가 없어 NOT_INSTALLED.
- 16:41:17 KST: 원하는 상태의 자원 15개 중 12개가 이미 존재. Namespace 3·ProxyGroup 3·Service 5·NetworkPolicy 1.
  NetworkPolicy 3개는 부재. 기존 12개에는 이 Application의 tracking 표식이 없었다.
- 이 관찰만으로 기존 자원의 소유자를 확정하거나 자동 인수를 승인하지 않았다.
  실제 고객의 자원·앱·DB·트래픽·권한은 변경하지 않았다.

## 운영 반영 전 인수점

위 15개 자원·namespace 생성 결과는 고정된 과거 커밋의 시험 기록이다. #632 이후 생성 차트는 Namespace를
만들지 않으므로 같은 결과를 현재 차트의 시험으로 옮기지 않는다. 현재 버전 재현은 T45 등 클러스터 구축
담당이 필요한 namespace를 먼저 만든 뒤 수행한다.

1. [기존 실행 창 조율](https://github.com/Softbank-Hackathon-Finals/platform/issues/36#issuecomment-6703631286)과
   고객 환경 담당의 설치·적용 시간을 맞춘다. 무응답을 동의로 보지 않는다.
2. 기존 12개 자원의 관리자와 원하는 상태의 diff를 확인하고, 같은 자원을 바꾸는 렌더러·세션을 정리한다.
3. 등록 UID·고정 커밋·차트 0.2.0 운영자 값으로 bootstrap만 설치한다. 플랫폼의 T43 Argo와 혼용하지 않는다.
4. 정지 상태로 Application을 등록하고 대상·소유권·외부 경로·권한을 검토한 뒤 활성화한다. 기본 prune=false를 유지한다.
5. 실제 고객에서 customer_status.py로 재조회한다. 별도로 T14/T19·업무·데이터 시험을 완료한다.

기존의 1단계 AppProject/controller 권한은 넓다. 고객별 pull 구조 자체가 최소 권한을 증명하지 않는다.
권한 세분화·커밋 서명·삭제 승인·운영 감사·복구는 별도 인수 조건이며 금융 운영 완료로 표시하지 않는다.

## #635 후속 리뷰 보완 — 차트 0.2.2

- `application.enabled`의 기본 false를 제거해 설치·업그레이드마다 명시를 요구한다. 생략한 예전 명령은
  Application 삭제로 이어지지 않고 렌더 단계에서 거부된다. Helm 스키마 검사를 건너뛴 경우도 같은 검사로 거부한다.
- 실행 중 재시도 횟수가 있으면 `SYNC_RETRYING`, 재시도가 끝나 이 커밋의 작업이 실패하면 `SYNC_FAILED`로 구분한다. 두 상태 모두 종료 코드 3이며 전환 권한을 주지 않는다.
- 차트 번호: #673이 0.2.1을 먼저 썼으므로 이 보완은 0.2.2다.
- 기존 과거 실험과 현재 namespace 선행 조건을 구분했다. 시계가 미래로 보이는 기록의 거부 조건은 완화하지 않았다.
- 검증은 IaC 시험 96개와 하위 시험 6개, 실제 고정 의존 차트의 Helm 렌더 시험 6개, Ruff·공개 문서 검사다.
  이번 보완을 실제 고객에 설치하거나 기존 고객 릴리스를 업그레이드하지 않았다.

근거: [#635 후속 리뷰](https://github.com/Softbank-Hackathon-Finals/platform/pull/635#pullrequestreview-5810924906).
