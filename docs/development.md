# Gitflow와 작업 번호

브랜치의 종류와 작업 번호를 나눠 표시한다. 일반 작업은 `feature/T1-aws-foundation`처럼 **`feature/` + `T번호` + 짧은 영문 설명**을 쓴다. 커밋·PR 제목은 기존 규칙인 `[T1] …`를 유지한다.

| 브랜치 | 시작점 | PR 대상 | 의미 |
|---|---|---|---|
| `feature/T번호-설명` | `develop` | `develop` | 개별 작업 |
| `release/vX.Y.Z` | `develop` | `main`, 이후 `develop` | 검증한 범위의 배포 준비와 수정 반영 |
| `hotfix/T번호-설명` | `main` | `main`, 이후 `develop` 또는 진행 중 release | 운영 버전 긴급 수정 |
| `bot/sync-tasks` | `develop` | `develop` | 이슈 체크·담당 동기화 |

`develop`은 통합 기준이며 GitHub 기본 브랜치다. `main`은 검증·릴리스 기준이다. 두 브랜치는 PR과 `secrets`·`infra`·`platform` 검사를 요구하며 force push·삭제를 막는다. release/hotfix를 main에 머지한 뒤 버전 태그를 남기고 수정이 develop에도 들어갔는지 확인한다. main에 넣었다는 이유만으로 클라우드 배포 성공을 표시하지 않는다.

일반 작업은 merge commit으로 합쳐 작업 묶음을 보존한다. `gh pr merge --auto --merge`를 사용한다. 검토 중인 타인의 브랜치를 강제로 다시 쓰지 않는다. 전환 전에 열린 PR은 기존 브랜치 이름을 보존할 수 있으며, 기능 PR의 대상만 develop으로 맞춘다.

```sh
git fetch origin
git switch -c feature/T1-aws-foundation origin/develop
# 구현, 변경에 맞는 로컬 검사, 실제 클라우드 검증, 증거 기록
git push -u origin feature/T1-aws-foundation
gh pr create --base develop --title '[T1] 작업 설명' --body-file /path/to/pr.md
gh pr merge --auto --merge
```

리뷰에는 문제·최종 동작·검증·남은 범위를 적는다. PR 전 로컬·클라우드 검증을 완료하며, 변경이 문서·CI뿐이면 그 범위의 정적 검사와 GitHub 설정 readback을 기록한다. 관련 없는 클라우드 자원을 만들어 검증했다고 쓰지 않는다.

이슈 체크는 기능 PR이 develop에 머지되고 해당 완료 기준을 확인한 뒤 한다. 제품 배포 여부는 release 기록으로 별도 확인한다. `sync-tasks`는 develop의 계획을 읽고 develop으로 봇 PR을 만든다. 기존 main의 설계 문서는 마지막 릴리스 기준이고, 다음 개발 기준은 develop의 같은 경로다.

브랜치 역할·release/hotfix의 양쪽 반영·merge commit 원칙은 [Vincent Driessen의 Git branching model](https://nvie.com/posts/a-successful-git-branching-model/)을 따른다. task prefix는 팀의 작업 추적 규칙이다.

칸반과 이슈·문서의 연동 및 로컬 명령은 [프로젝트 보드 안내](project-board.md)를 따른다.
