# Gitflow와 작업 번호

이 문서의 브랜치·병합 절차는 `platform` 저장소에 적용한다. `demo-chat`은 [자체 AGENTS.md](https://github.com/Softbank-Hackathon-Finals/demo-chat/blob/main/AGENTS.md)와 해당 브랜치의 실제 보호 규칙을 따른다. 같은 CI 이름을 사용해도 저장소별 승인 요건이 같다는 뜻은 아니다.

## 담당 태스크를 넘는 변경

공식 작업 담당은 현재 이슈 Assignees로 확인한다. PR 작성자, 로컬 세션의 worktree 등록, 독립 검토자는 서로 다른 역할이며 공식 담당을 대체하지 않는다.

1. 다른 사람이 담당하는 열린 태스크의 코드를 바꾸기 전에 담당자에게 변경 이유와 범위를 알리고 합의한다. 기존 이슈·PR·Slack 스레드에 범위와 근거 링크를 남긴다. 이미 합의된 범위는 다시 승인받지 않아도 된다.
2. 통합 작업에서는 인프라 준비와 다른 태스크의 핵심 로직 변경을 PR로 나누고, 로직 변경은 그 태스크 담당의 리뷰를 받는다. 공통 인프라의 준비 요청만으로 실행기·판정기까지 변경할 권한이 생기지 않는다.
3. 로컬 세션 간 배정, 리뷰 요청, CI 성공, 사후 코드 승인은 변경 전 담당자와의 범위 합의를 대신하지 않는다. 범위 밖 작업을 발견하면 그 부분의 구현을 멈추고 조율하되, 합의된 독립 작업과 안전한 실행 마무리는 계속한다.
4. 이미 병합된 변경은 인프라/로직별 변경점·검증 근거·남은 연결 조건을 인계한다. 과거 PR을 뒤늦게 분리했다고 표시하지 않는다. 이후 변경은 합의된 담당 범위에서 진행한다.
5. 개인 SSO와 EKS/RKE2 접근 인계는 별도 선행 조건이다. 운영자·CI 계정의 성공을 담당자 본인 접속 완료로 대신하지 않으며, 접근 대기 자체가 제품 태스크 소유권을 다른 사람에게 넘기지는 않는다.

11/4 T12 조율 사례에서는 DB 제어 함수·CNPG·PgBouncer·CA·실환경 준비를 T6/T7/T30 인프라 범위에서 제공하고, 비교기 연결·1012 호출·실패 처리·배포·실제 컷오버는 T12 담당이 이어받기로 했다. T30 등의 공식 Assignee 변경을 의미하지 않는다. 근거: [규태님의 범위 제안](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793721616310219), [수용·인계 답변](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793721922875629). 사람 접근과 실제 인수의 완료 여부는 해당 작업에서 확인한다.

## 브랜치와 병합

브랜치의 종류와 작업 번호를 나눠 표시한다. 일반 작업은 `feature/T1-aws-foundation`처럼 **`feature/` + `T번호` + 짧은 영문 설명**을 쓴다. 커밋·PR 제목은 기존 규칙인 `[T1] …`를 유지한다.

| 브랜치 | 시작점 | PR 대상 | 의미 |
|---|---|---|---|
| `feature/T번호-설명` | `develop` | `develop` | 개별 작업 |
| `release/vX.Y.Z` | `develop` | `main`, 이후 `develop` | 검증한 범위의 배포 준비와 수정 반영 |
| `hotfix/T번호-설명` | `main` | `main`, 이후 `develop` 또는 진행 중 release | 운영 버전 긴급 수정 |
| `bot/sync-tasks` | `develop` | `develop` | 이슈 체크·담당 동기화 |

`develop`은 통합 기준이자 기본 브랜치이고 `main`은 검증·릴리스 기준이다. 일반 작업은 merge commit으로 합쳐 작업 묶음을 보존한다. release/hotfix를 main에 머지한 뒤 버전 태그와 develop 반영을 확인한다. 병합은 클라우드 배포 성공을 뜻하지 않는다.

2026-11-04 확인한 적용 규칙은 다음과 같다. 실행 직전 GitHub의 현재 규칙을 다시 읽는다.

| 대상 | 숫자로 지정한 최소 승인 | 코드 오너 승인 | 필수 CI |
|---|---|---|---|
| platform `develop` | 0 | 필수. 오너가 있는 파일을 바꾸면 적격한 팀원 승인 필요 | `secrets` · `infra` · `platform` |
| platform `main` | 1 | 별도 필수 설정 없음 | `secrets` · `infra` · `platform` |

`.github/CODEOWNERS`는 `docs/tasks.md`를 제외한 경로에 팀원 전원을 지정한다. 따라서 develop의 숫자 0은 코드 PR의 승인 면제를 뜻하지 않는다. `docs/tasks.md`만 바꾼 PR은 오너가 없어 필수 CI 통과로 병합할 수 있다. 사람의 작업 계획 PR은 GitHub 기본 auto-merge를 예약할 수 있고, 봇 `[sync]` PR은 자동 예약 없이 팀원이 머지 버튼으로 반영한다. 근거: [실제 적용·검증 안내](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793714314857809).

리뷰는 [해당 파트의 주·부 담당](review-routing.md)이 맡고 전원 승인을 기다리지 않는다. 변경 요청을 해소하고 최종 head의 필수 CI·현재 승인 조건을 충족해야 한다. 새 커밋에 이전 승인을 그대로 적용하지 않는다. GitHub 기본 auto-merge는 이 조건을 집행하는 경로이며 별도 봇·자기 승인·리뷰 무효화로 보호 규칙을 우회하지 않는다. 검토 중인 타인의 브랜치를 강제로 다시 쓰지 않는다. 전환 전에 열린 PR의 기존 브랜치 이름은 보존할 수 있다.

```sh
git fetch origin
git switch -c feature/T1-aws-foundation origin/develop
# 구현, 변경에 맞는 로컬 검사, 실제 클라우드 검증, 증거 기록
git push -u origin feature/T1-aws-foundation
gh pr create --base develop --title '[T1] 작업 설명' --body-file /path/to/pr.md
# docs/review-routing.md에서 T번호의 주·부 참여자를 확인해 작성자 외 모두 요청.
# 필수 CI · 대상 브랜치의 현재 승인 조건 · 변경 요청 해소를 확인한 뒤 병합:
gh pr merge --auto --merge --match-head-commit <검증한-HEAD-SHA>
```

리뷰에는 문제·최종 동작·검증·남은 범위를 적는다. PR 전 로컬·클라우드 검증을 완료하며, 변경이 문서·CI뿐이면 그 범위의 정적 검사와 GitHub 설정 readback을 기록한다. 관련 없는 클라우드 자원을 만들어 검증했다고 쓰지 않는다.

이슈 체크는 기능 PR이 develop에 머지되고 해당 완료 기준을 확인한 뒤 한다. 제품 배포 여부는 release 기록으로 별도 확인한다. `sync-tasks`는 develop의 계획을 읽고 develop으로 봇 PR을 만든다. 기존 main의 설계 문서는 마지막 릴리스 기준이고, 다음 개발 기준은 develop의 같은 경로다.

브랜치 역할·release/hotfix의 양쪽 반영·merge commit 원칙은 [Vincent Driessen의 Git branching model](https://nvie.com/posts/a-successful-git-branching-model/)을 따른다. task prefix는 팀의 작업 추적 규칙이다.

칸반과 이슈·문서의 연동 및 로컬 명령은 [프로젝트 보드 안내](project-board.md)를 따른다.

리뷰 요청자는 [파트별 리뷰 기준과 근거](review-routing.md)를 따른다. 파트 5의 전원 협업, 개별 작업 Assignee, 초기 지원 댓글과 후속 배정을 구분한다.

전환 전 PR의 보드 연결은 `scripts/sync_tasks.py`의 `LEGACY_PRS`에 번호·T번호·브랜치를 명시한 경우만 허용한다. 현재 예외는 #51 / T5 / `t5-control-contract`이며, 같은 저장소·develop 대상 조건을 그대로 검사한다. 임의 브랜치를 제목만으로 연결하지 않는다.
