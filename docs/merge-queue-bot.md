# 기존 Actions를 재사용하는 큐 등록 봇

T41은 T40의 GitHub 기본 Merge Queue 앞에서 **등록만 자동화**한다. 새 GitHub 계정·App·키를 만들지 않는다. 기존 `sync-tasks`와 같은 GitHub Actions 체계와 저장소의 `PROJECT_TOKEN`을 재사용하며 보드 동기화 코드·Slack 설정은 바꾸지 않는다. PAT 인증의 실제 API 행위자는 그 기존 토큰의 소유자다. 새 전용 봇 계정이 생기는 것은 아니다.

## 일반 PR의 흐름

```text
feature → develop PR
→ 필수 CI 완료 또는 ready/변경 이벤트
→ 현재 GitHub 상태와 head SHA 재조회
→ enqueuePullRequest(expectedHeadOid)
→ GitHub가 merge_group CI를 실행
→ GitHub 기본 큐가 통과한 커밋을 병합
```

리뷰 요청 알림이나 미제출 리뷰는 승인 대기로 취급하지 않는다. 사람 승인 1건을 새로 요구하지 않는다. `main`, 다른 저장소·fork, `bot/` 브랜치는 자동 등록 대상이 아니다. 기존 `[sync]` PR의 팀원 수동 큐 등록 정책은 유지한다.

## 판정과 경계

| 상태 | 동작 |
|---|---|
| 같은 저장소의 일반 develop PR, 필수 CI 성공, 충돌 없음 | head를 다시 확인하고 큐 등록 |
| 이미 큐에 있음 | 추가 요청 없음 |
| Draft·명시적 수정 요청·충돌·`merge-queue:hold`/`do-not-merge` | 등록 보류. 기존 예약/큐가 있으면 취소 시도 |
| 검사 누락·실패·취소·skip·확인 중 | 등록하지 않음 |
| 현재 head가 이전 큐에서 제외됨 | 같은 head 자동 재등록 없음. 원인 확인 후 GitHub에서 직접 재등록하거나 새 커밋으로 진행 |
| 이벤트 head와 현재 head가 다름 | 오래된 이벤트 무시 |
| 큐 규칙 없음·필수 CI 축소·조회 실패·이력 일부만 조회됨 | UNKNOWN/실패, 변경하지 않음 |

GitHub MERGE 그룹의 마지막 부모가 해당 PR head인 것을 큐 제외 이력과 대조한다. 과거 실패를 지우는 label·DB를 별도로 만들지 않는다. `merge_group` 완료 이벤트 자체는 새 등록을 만들지 않는다. 이벤트 병합으로 놓친 PR은 10분 주기 재조회로 복구하되 같은 head의 제외 이력은 계속 존중한다.

라벨은 등록 전 필터이며 GitHub의 원자적 병합 차단 규칙이 아니다. 이미 큐에 있는 PR을 즉시 멈춰야 하면 GitHub에서 직접 큐 제외 또는 Draft 전환을 사용한다. 봇의 비동기 취소가 끝날 때까지 병합이 멈춘다고 보장하지 않는다.

## 인증과 코드 신뢰

- `merge-queue.yml`은 기존 `PROJECT_TOKEN`을 한 실행 단계에서만 사용한다. 값은 조회·출력·파일 저장하지 않는다. 새 키나 권한을 발급하지 않는다.
- 기본 `GITHUB_TOKEN` 권한은 `contents: read`다. PROJECT_TOKEN의 기존 권한을 확대하지 않는다.
- 고정 SHA의 checkout action으로 `github.workflow_sha`만 읽고 checkout 자격을 보존하지 않는다. PR head·fork 코드·아티팩트·모델 출력은 실행하지 않으며 패키지 설치도 하지 않는다.
- PR 번호·head·상태·검사는 GitHub API에서 읽는다. 제목·본문·브랜치 이름을 shell로 실행하지 않는다. API는 고정 argv와 JSON stdin만 사용한다.
- 실행 변경은 develop의 이 workflow, 활성화 변수, `--apply`를 모두 요구한다. 로컬 기본 실행과 수동 dry-run은 조회만 한다.
- `mergePullRequest`, admin bypass, 리뷰 승인·dismiss, 원 브랜치 push, GitHub ruleset 변경은 봇에 구현하지 않는다.

## 활성화와 확인

구현 병합만으로 활성화됐다고 하지 않는다. `MERGE_QUEUE_BOT_ENABLED`가 정확히 `true`일 때 자동 이벤트가 실행된다.

1. 기능 PR의 회귀·필수 CI와 실제 큐 병합을 확인한다.
2. develop에서 `merge-queue` workflow를 `dry-run=true`로 실행한다. 일반 PR의 판정과 기존 보류 PR의 미변경을 확인한다.
3. 별도 합성 PR로 기존 인증이 실제 `enqueuePullRequest`를 수행하고, 뒤따른 `merge_group` CI와 병합 SHA가 연결되는지 확인한다. 가짜 status를 쓰지 않는다.
4. 적용 결과·실행 URL·미검증 범위를 T41 이슈에 기록하고 활성화 상태를 다시 조회한다.

```sh
gh workflow run merge-queue.yml --ref develop -f dry-run=true
# 특정 PR만 조회:
gh workflow run merge-queue.yml --ref develop -f dry-run=true -f pull_request=123
```

`--apply` 없는 `python3 scripts/merge_queue.py --pr 123`도 조회 전용이다. 상태 summary에는 PR 번호·head·고정 결과 코드만 기록하며 댓글을 매번 만들지 않는다.

## 중단과 복구

`MERGE_QUEUE_BOT_ENABLED=false`는 **새 자동 실행을 중단**한다. 진행 중인 등록 workflow는 별도로 취소하고 이미 GitHub 큐에 들어간 항목은 필요한 것만 직접 제외한다. 큐 보호 규칙·필수 CI·main 정책·제품 실행 승인·보드·Slack 설정을 끄는 방법으로 복구하지 않는다. 토큰 오류는 실패로 표시하며 키 값을 요청하거나 로그에 남기지 않는다.

현재 큐 정책은 [T40 운영 절차](merge-queue.md)를 따른다. 실제 코드·실행 기록은 T41 이슈에서 추적한다.

## 근거

- [GitHub Merge Queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- [워크플로 간 이벤트와 토큰](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow)
- [enqueuePullRequest API](https://docs.github.com/en/graphql/reference/mutations#enqueuepullrequest)
