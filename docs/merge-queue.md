# develop Merge Queue 운영

T40은 회의에서 확정한 **develop 승인 필수 해제 + 필수 CI 기반 통합**을 구현한다. 회의 결정이 이전 선택적 승인 제안보다 우선한다. 특정 위험 경로의 필수 승인 규칙을 따로 남기지 않는다. main과 제품의 계획·배포·컷오버 승인은 변경하지 않는다.

## 병합 기준

1. feature 브랜치에서 develop으로 PR을 올리고 파트 담당에게 검토를 요청한다. CODEOWNERS는 알림 용도다.
2. 실제 head의 필수 CI와 현재 지적·충돌·draft 여부를 확인한다. 알려진 결함이 있으면 수정하거나 큐에서 제외한다.
3. `gh pr merge <번호> --auto --merge --match-head-commit <head>`로 GitHub 기본 경로를 사용한다. 준비 전이면 auto-merge 예약, 준비됐으면 큐 등록으로 처리된다. 봇 `[sync]` PR은 팀원이 등록한다.
4. 큐는 최신 develop과 앞선 PR을 합친 SHA에서 `secrets`, `infra`, `platform`을 다시 검사한다. PR 검사만 성공한 상태로 직접 병합할 수 없다.
5. 충돌·실패·누락·시간 초과는 큐의 제거·대기 결과를 확인하고 해당 PR에서 수정한다. 무조건 재등록하는 루프를 두지 않는다.

변경 요청 이력은 삭제하지 않는다. 승인 필수 해제는 검토에서 확인한 결함을 무시하라는 의미가 아니다. 기존 미해결 PR의 auto-merge 예약은 설정 전환 전에 해제한다. 다른 담당의 핵심 코드는 사전 범위 합의 후 수정한다.

## 설정과 권한

| 항목 | 설정 |
|---|---|
| 대상 | platform `refs/heads/develop`만 |
| 승인 | 숫자 0, 코드 오너 승인 요구 해제 |
| 큐 검사 | ALLGREEN, MERGE 방식 |
| 병렬 빌드 | 최대 2개 |
| 병합 묶음 | 최소 1, 최대 2개; 최소 대기 0분 |
| 필수 검사 응답 제한 | 60분 |
| 필수 CI | secrets · infra · platform |
| 삭제·강제 push·bypass | 기존 금지 유지, bypass 사용자 추가 없음 |
| main | 기존 ruleset 유지 |

CI는 `contents: read`만 사용하고 클라우드·모델 자격 증명을 주입하지 않는다. `merge_group` 이벤트의 base/head와 checkout의 일치를 검증한다. 그룹 전체 diff를 검사하며 앞선 PR의 변경을 생략하지 않는다. 실패한 scope나 실행돼야 할 job의 skip을 성공으로 해석하지 않는다. GitHub 자체 큐를 사용하며 별도 merge 봇·자기 승인·가짜 status를 만들지 않는다.

## 전환 순서와 확인

- 기존 develop/main ruleset과 열린 PR 상태를 비공개로 백업한다.
- 미해결 PR의 자동 병합 예약을 해제한다. Slack 봇은 재활성화하지 않는다.
- merge_group 지원 CI와 이 규약을 **기존 보호 규칙 아래** 병합한다.
- 별도 소유 시험 브랜치에 실제 큐를 붙여 승인 없는 병합과 실패 차단을 확인한다. 시험용 실패 코드는 develop에 넣지 않는다.
- develop에만 설정을 적용하고 readback으로 승인 0·codeowner false·큐·필수 CI·bypass 없음을 확인한다. main 백업과 현재 규칙을 비교한다.
- 실제 그룹의 SHA·Actions run·병합/차단 결과를 T40 이슈에 기록한다. 검증 전 항목은 완료로 체크하지 않는다.
- 기존 PR은 최신 head/CI/지적/범위를 기준으로 개별 처리한다. 승인 대기를 없앤 것과 코드 결함 해결을 구분한다.

## 장애 대응

큐 CI가 시작되지 않으면 `merge_group` 트리거와 head의 워크플로를 확인한다. 필수 결과를 임의 성공 처리하지 않는다. 큐 전체가 막히면 신규 등록을 중단하고 원인을 수정한다. 정책 복구가 필요하면 저장한 이전 develop 규칙을 복원하되 CI와 PR 보호를 유지한다. 삭제·bypass를 켜서 우회하지 않는다.

코드 revert가 이미 실행한 DB 변경이나 데이터 복구를 대신하지 않는다. 통합 시연은 고정 commit·설정으로 수행하고 실제 데이터·권한·복구 결과를 별도로 기록한다. 실행 중 운영 변경은 기존 승인·담당 범위에서만 수행한다.

## 근거

- [GitHub Merge Queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- [merge_group 이벤트](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#merge_group)
- [REST ruleset 설정](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset)
- [개발 규약](development.md), [CI 안내](ci.md), [ADR-0017](adr/0017-develop-merge-queue.md)
