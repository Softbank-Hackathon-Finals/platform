# T41 기존 인증 자동 큐 등록 인수 결과

새 봇 계정·App·키 없이 기존 `sync-tasks`의 `PROJECT_TOKEN`을 재사용했다. GitHub Actions가 등록 판정을 수행하고, 실제 병합은 GitHub 기본 Merge Queue가 담당한다. PAT API의 행위자는 기존 토큰의 소유자이며 새 전용 봇 계정이 아니다.

- 구현: [PR #261](https://github.com/Softbank-Hackathon-Finals/platform/pull/261), head `e493e75ed9896a81a9c813c9e519db02d96db520`
- 실제 인수 시험: [PR #263](https://github.com/Softbank-Hackathon-Finals/platform/pull/263), head `be1490b2d617645652909be73d1685c0a73d982c`
- 작업: [T41 #262](https://github.com/Softbank-Hackathon-Finals/platform/issues/262)
- 확인일: 2026-11-05, 시각은 KST

## 실제 시험

| 단계 | 확인 결과 | 근거 |
|---|---|---|
| 구현 병합 | 실제 큐 CI 성공 후 `1f89156e4cb7a629b12ee1fcf721c01dd3e556e6`으로 병합 | [merge_group #41967562191](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41967562191) |
| 기존 인증 조회 | 원래 수정 요청 PR 6개는 HELD, sync PR은 SYNC_MANUAL. 변경 없이 정상 종료 | [dry-run #41968087352](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41968087352) |
| Draft 보류 | 시험 PR의 필수 secrets/infra/platform 성공 후에도 Draft 상태와 큐 미등록 확인 | [PR CI #41968279367](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41968279367), [HELD #41968279373](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41968279373) |
| Ready 자동 등록 | Ready 전환 후 기존 인증이 현재 head를 자동 enqueue. 수동 등록·승인 리뷰 없음 | [등록 #41968485420](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41968485420) |
| 실제 큐 병합 | 큐 CI 성공 후 13:34:17에 `6907b59c87bc9d111b65d1f7a844f2c43ce68b48`으로 병합. 리뷰 제출 0건 | [merge_group #41968535666](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41968535666), [PR #263](https://github.com/Softbank-Hackathon-Finals/platform/pull/263) |

위 시험에서 가짜 status·관리자 우회·리뷰 해제·보호 규칙 완화는 사용하지 않았다. 이미 실패한 동일 head의 재등록 차단, 오래된 이벤트, 동시 head 변경과 불완전한 조회 등은 전용 회귀 시험 24개로 확인했다. T40의 실제 실패 그룹 차단 시험과 T41의 회귀 시험을 T41이 새로 실제 장애 주입한 결과로 혼동하지 않는다.

## 적용 상태와 보존 범위

- `MERGE_QUEUE_BOT_ENABLED=true` 활성화와 적용 후 조회를 확인했다.
- 적용 후에도 #144·#207·#210·#251·#257·#258은 HELD, `bot/sync-tasks`는 SYNC_MANUAL이었다. 이 상태는 확인 시점의 기록이다.
- main ruleset은 적용 전 JSON과 동일했다. 기존 Slack 채널 설정은 계속 없으며 Slack 봇을 재활성화하지 않았다.
- 제품의 계획·배포·컷오버 승인과 AWS 권한은 이 작업의 변경 범위에 포함되지 않는다.
- 상시 운영·토큰 만료·GitHub 장기 장애의 모든 경우를 실환경에서 시험했다는 의미는 아니다. 조회·등록 결과가 불명확하면 성공으로 처리하지 않는다.

활성화·중단·수동 재시도와 비동기 보류의 한계는 [운영 절차](../../merge-queue-bot.md)를 따른다.
