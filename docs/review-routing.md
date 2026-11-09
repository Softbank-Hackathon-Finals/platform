# 파트별 리뷰 요청과 역할 근거

확인 기준: 2026-11-03 KST. 사용자 지시: 해당 파트의 **주·부 참여자를 모두** 리뷰 후보에 포함한다. 구현 책임자 한 명과 파트 리뷰 대상을 구별한다. 작성자 자신은 요청 대상에서 제외한다.

## 1. 어느 자료를 기준으로 실행하는가

- 현재 설계·영역 담당은 저장소 `docs/design.html` §11.2, 작업 계획·영역은 `docs/tasks.md`, 현재 작업 담당은 GitHub 이슈 Assignees로 확인한다. 팀 운영 규칙의 원본도 저장소다. [WorkGuide](https://app.notion.com/p/40c8bee9ada4802fb712d2df18eeb6e0)
- 허들 댓글은 당시 지원·변경 논의를 확인하는 근거다. 후속 회의 정리와 저장소에 반영된 배정이 있으면 댓글만으로 현재 배정을 되돌리지 않는다.
- 문서의 최종 편집 시각은 그 문장에 대한 결정 시각으로 간주하지 않는다. 아래는 메시지 발송 시각과 실제 저장소 반영 시각을 구분했다.
- 역할은 협의해서 조정할 수 있다. 현재 배정과 다른 역할을 맡을 때는 산출물·접점·담당 변경을 기록한다. 댓글과 후속 문서가 다르다는 사실만으로 어느 쪽을 오타라고 단정하지 않는다.

## 2. 류지환의 3번/5번 차이 — 양측 근거

| 시점(KST) | 근거 | 확인된 내용 | 현재 적용 |
|---|---|---|---|
| 11/2 21:47:48 | [본인 허들 댓글](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793623668157169) | `주업무: 4번`, `부업무: 3번` | 초기 지원 이력 |
| 11/2 22:00:54 | [허들 내부 정리표](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793624454334049) | 파트 3에 류지환(부), 파트 4에 류지환(주) | 초기 정리 이력 |
| 11/2 회의 내용, 사후 기록 | [회의록 §10 역할 분담](https://app.notion.com/p/40b8bee9ada4800785ead22138aecd9e) | 류지환이 부 업무를 **3→5로 옮기는 안**을 냈다고 기록. 배규태가 정리해 Slack에 공유하기로 함 | 변경 논의를 뒷받침. 제안 문장만으로 확정하지 않음 |
| 11/2 22:08:45 | [채널에 공유한 후속 역할표](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793624925241849) | 파트 3은 배규태·전동훈, 파트 4는 김태현·류지환(주), 파트 5는 이상무·류지환(부) | 후속 정리 |
| 11/3 00:11:55 | [설계·작업 배정 PR #4 병합](https://github.com/Softbank-Hackathon-Finals/platform/pull/4), commit `49a6440` | 저장소 정본에 **4(주)·5(부)** 반영 | 현재 영역 기준 |
| 현재 조회 | [태스크 분배 기준](https://app.notion.com/p/40c8bee9ada4804ab75ae321d389f720), [T10 이슈 #19](https://github.com/Softbank-Hackathon-Finals/platform/issues/19) | 분배 문서도 4(주)·5(부), T10 Assignee는 류지환 | 현재 세부 작업 배정과도 일치 |

**현재 적용은 류지환 4번 주·5번 부다.** 근거는 단순히 표가 늦게 올라왔다는 사실만이 아니다. 변경 논의가 회의록에 있고, 후속 공유표·병합된 설계·작업 계획·실제 이슈 담당이 같은 방향이다. 초기 4번 주·3번 부 댓글은 지우거나 없었던 것으로 설명하지 않는다.

기존 분석에서 '모든 자료가 일치한다'고 쓴 것은 잘못이었다. 초기 댓글과 허들 내부 표는 후속 배정과 다르며, 그 변경 경위를 위처럼 구별한다.

## 3. 팀 전체의 주·부와 현재 작업

회의록은 **주 업무 1개 + 부 업무 1개**로 분배했다고 적는다. 류지환의 주·부는 후속 표와 정본에 명시돼 있다. 다른 네 명은 마지막 개인 댓글의 두 번호를 이 순서로 읽어 아래에 재구성했다. **그 네 명의 순서는 댓글 해석이며, 정본에 주/부가 개별 표시된 사실과 구별한다.** 리뷰 요청은 순서와 무관하게 해당 파트 참여자를 모두 포함한다.

| 팀원 / GitHub | 주 | 부 | 근거 수준 | 현재 직접 할당 |
|---|---|---|---|---|
| 류지환 `mangowhoiscloud` | **4 인프라·CI·Edge** | **5 플랫폼 API·콘솔·데모 앱** | 후속 역할표·정본 명시 | T1, T6, T7, T10, T17, T38 |
| 김태현 `kth4778` | 1 데이터·컷오버 | 4 인프라·CI·Edge | [최종 댓글 `1, 4`](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793623658354629)의 순서 해석 | T2, T8, T13, T18, T27, T30 |
| 배규태 `baekyutae` | 1 데이터·컷오버 | 3 에이전트·리허설 | [댓글 `1,3`](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793623652233439)의 순서 해석 | T3, T12, T15, T20, T25, T26 |
| 전동훈 `Jeonsubb` | 3 에이전트·리허설 | 2 보안 통제·게이트 | [마지막 댓글 `3, 2`](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793624352883729)의 순서 해석. 앞선 `1, 4`는 대체됨 | T9, T11, T16, T24, T31, T32 |
| 이상무 `sangmu1126` | 2 보안 통제·게이트 | 5 플랫폼 API·콘솔·데모 앱 | [마지막 댓글 `2, 5`](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793623742237299)의 순서 해석. 앞선 `2 4`는 대체됨 | T4, T5, T14, T19, T21, T22, T23, T33, T34, T35, T36, T37 |

**파트 5는 팀 전체가 함께 완성하는 영역**이라는 사용자 확인을 적용한다. 회의록에도 각자 개발한 뒤 통합해야 한다고 적혀 있다. 정본에 명시된 이상무·류지환과 T4/T10/T21/T22의 개별 Assignee는 구체적인 실행 책임이며, 다른 팀원을 파트 5 협업에서 제외하는 뜻이 아니다. 파트 5 PR의 기본 리뷰 후보는 전원이다.

작업 밖 역할은 류지환의 병합·E2E·AWS/GCP 계정 관리, 분배 문서상 T28 리드, 배규태의 설계 문서, 이상무의 관측(지표·OTel)이다. T28·T29 이슈는 전원 공통이다. T11·T15·T26의 류지환 자문은 분배 문서의 '함께 정할 것'에 남아 있어 파트 3의 고정 배정을 추가하지 않는다.

## 4. 파트 → 리뷰 후보

| 파트 | 후보(주·부 모두, 작성자 제외 전) | 현재 작업 |
|---|---|---|
| 1 데이터·컷오버 | 배규태 `baekyutae`, 김태현 `kth4778` | T8, T12, T13, T18, T20, T25, T27 |
| 2 보안 통제·게이트 | 이상무 `sangmu1126`, 전동훈 `Jeonsubb` | T5, T9, T14, T19, T23, T24, T33, T34, T35 |
| 3 에이전트·리허설 | 배규태 `baekyutae`, 전동훈 `Jeonsubb` | T3, T11, T15, T16, T26, T31, T32 |
| 4 인프라·CI·Edge | 김태현 `kth4778`, 류지환 `mangowhoiscloud` | T1, T2, T6, T7, T17, T30, T38 |
| 5 플랫폼 API·콘솔·데모 앱 | 전원. 실행상 명시 담당은 이상무·류지환 | T4, T10, T21, T22, T36, T37 |
| 공통 통합·문서 | 전원 | T28, T29 |

요청 절차:

1. PR의 T번호를 실제 변경 범위와 대조하고 `docs/tasks.md`의 영역을 확인한다.
2. 해당 파트의 주·부 참여자 합집합에서 PR 작성자를 뺀다. 여러 파트의 구현·공통 계약을 실제로 바꾸면 그 파트도 포함한다.
3. 이미 요청됐거나 해당 변경을 검토 중인 사람에게 중복 알림을 보내지 않는다. 제출된 리뷰를 리뷰 대상 정리라는 이유로 삭제·무효화하지 않는다.
4. 요청자 목록을 API로 재조회한다. 리뷰 요청은 승인과 다르며 전원 승인을 기다리지 않는다. 병합 조건·tasks.md 예외·봇 PR 처리는 [개발 규약](development.md#브랜치와-병합)과 실제 GitHub ruleset을 따른다. 다른 담당의 로직 변경은 [사전 범위 합의](development.md#담당-태스크를-넘는-변경)와 그 담당의 리뷰가 필요하다. 리뷰 후보 선정이나 코드 승인은 사전 합의를 대신하지 않는다.
5. 선행·후속 이슈라는 이유만으로 모든 담당자를 추가하지 않는다. 반대로 R&R 밖의 협업·리뷰 제안을 막지도 않는다. 추가 요청에는 영향을 받는 계약이나 검토 이유를 적는다.

`.github/CODEOWNERS`는 모든 파일의 오너를 팀원 전원으로 두어 PR마다 전원을 리뷰어로 자동 지정한다(2026-11-03 팀 합의: [제안](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793707496460339) · [공지](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793711623617629)). 리뷰는 위 기준의 파트 담당이 맡고 나머지는 알림만 받는다. 2026-11-07 11:52 KST부터 develop·main 모두 필수 승인과 Merge Queue가 없고, 필수 검사가 통과하면 직접 병합한다([개발 규약](development.md#브랜치와-병합)). CODEOWNERS는 리뷰 알림 용도다. T번호 기반 자동 리뷰어 배정은 아직 구현하지 않았으며 이 문서에 따라 명시적으로 요청한다.

## 5. T1~T38별 실행 담당과 리뷰 후보

아래는 2026-11-03 이슈 조회 결과다. 이후 담당이 바뀌면 이슈 Assignees를 먼저 다시 확인한다. 후보 칸은 **작성자 제외 전**이며, 부 역할도 포함한다. T33~T35는 초기 설계표 이후 추가된 보안 파트 작업이다. T36·T37은 11/4 확인한 `tasks.md`의 플랫폼 파트 작업으로 보완했다. 담당 변경은 이 표를 단독 근거로 실행하지 않는다.

| 작업 | 이름 | 파트 | 현재 이슈 담당 | 리뷰 후보 |
|---|---|---|---|---|
| [T1 · #5](https://github.com/Softbank-Hackathon-Finals/platform/issues/5) | AWS 기반: 권한 · VPC 2개 · 비용 태그 | 4 | 류지환 | 김태현 · 류지환 |
| [T2 · #6](https://github.com/Softbank-Hackathon-Finals/platform/issues/6) | 온프렘 RKE2 (Ubuntu VM, CIS) + 공통 cloud-init | 4 | 김태현 | 김태현 · 류지환 |
| [T3 · #7](https://github.com/Softbank-Hackathon-Finals/platform/issues/7) | 리허설 네임스페이스 격리 실측 | 3 | 배규태 | 배규태 · 전동훈 |
| [T4 · #8](https://github.com/Softbank-Hackathon-Finals/platform/issues/8) | 데모 앱 v0: 채팅 스텁 | 5 | 이상무 | 전원 — 명시 담당 이상무 · 류지환 |
| [T5 · #9](https://github.com/Softbank-Hackathon-Finals/platform/issues/9) | 6개 통제 시험 설계 | 2 | 이상무 | 이상무 · 전동훈 |
| [T6 · #11](https://github.com/Softbank-Hackathon-Finals/platform/issues/11) | 출발지: EKS + CNPG + PgBouncer + ESO | 4 | 류지환 | 김태현 · 류지환 |
| [T7 · #12](https://github.com/Softbank-Hackathon-Finals/platform/issues/12) | 플랫폼 클러스터 (EC2 + RKE2) + Actions OIDC | 4 | 류지환 | 김태현 · 류지환 |
| [T8 · #18](https://github.com/Softbank-Hackathon-Finals/platform/issues/18) | 복제 경로: 논리 복제 왕복 · 첨부 동기화 | 1 | 김태현 | 배규태 · 김태현 |
| [T9 · #13](https://github.com/Softbank-Hackathon-Finals/platform/issues/13) | 목적지 보안 구성요소: OpenBao · ESO · SeaweedFS | 2 | 전동훈 | 이상무 · 전동훈 |
| [T10 · #19](https://github.com/Softbank-Hackathon-Finals/platform/issues/19) | 상태 머신 + 플랫폼 API + 이벤트 스트림 | 5 | 류지환 | 전원 — 명시 담당 이상무 · 류지환 |
| [T11 · #20](https://github.com/Softbank-Hackathon-Finals/platform/issues/20) | 에이전트 워커 + 도구 계층 + 제안서 스키마 | 3 | 전동훈 | 배규태 · 전동훈 |
| [T12 · #22](https://github.com/Softbank-Hackathon-Finals/platform/issues/22) | Cutover Executor | 1 | 배규태 | 배규태 · 김태현 |
| [T13 · #23](https://github.com/Softbank-Hackathon-Finals/platform/issues/23) | 무결성 비교와 영수증 | 1 | 김태현 | 배규태 · 김태현 |
| [T14 · #21](https://github.com/Softbank-Hackathon-Finals/platform/issues/21) | 통제 시험기와 통제 비교표 | 2 | 이상무 | 이상무 · 전동훈 |
| [T15 · #24](https://github.com/Softbank-Hackathon-Finals/platform/issues/24) | 리허설 러너: 작업 → 검증 Job → 승인 결속 | 3 | 배규태 | 배규태 · 전동훈 |
| [T16 · #25](https://github.com/Softbank-Hackathon-Finals/platform/issues/25) | 의존성 · 설정 인벤토리: 정적 탐지 + 설정 수집 + 3등급 분류 | 3 | 전동훈 | 배규태 · 전동훈 |
| [T17 · #26](https://github.com/Softbank-Hackathon-Finals/platform/issues/26) | Edge: Cloudflare LB + Tunnel 가중치 전환 | 4 | 류지환 | 김태현 · 류지환 |
| [T18 · #14](https://github.com/Softbank-Hackathon-Finals/platform/issues/14) | 부하 생성기와 관측 | 1 | 김태현 | 배규태 · 김태현 |
| [T19 · #29](https://github.com/Softbank-Hackathon-Finals/platform/issues/29) | 원본 대조 게이트 | 2 | 이상무 | 이상무 · 전동훈 |
| [T20 · #30](https://github.com/Softbank-Hackathon-Finals/platform/issues/30) | 되돌리기 (failback) | 1 | 배규태 | 배규태 · 김태현 |
| [T21 · #27](https://github.com/Softbank-Hackathon-Finals/platform/issues/27) | 콘솔 | 5 | 이상무 | 전원 — 명시 담당 이상무 · 류지환 |
| [T22 · #15](https://github.com/Softbank-Hackathon-Finals/platform/issues/15) | 데모 앱 v1 | 5 | 이상무 | 전원 — 명시 담당 이상무 · 류지환 |
| [T23 · #16](https://github.com/Softbank-Hackathon-Finals/platform/issues/16) | 규제 매핑표 원문 확인 | 2 | 이상무 | 이상무 · 전동훈 |
| [T24 · #28](https://github.com/Softbank-Hackathon-Finals/platform/issues/28) | 감사 로그 (S3 Object Lock) | 2 | 전동훈 | 이상무 · 전동훈 |
| [T25 · #35](https://github.com/Softbank-Hackathon-Finals/platform/issues/35) | 반복 측정 · 장애 주입 · SAFE_HOLD 시험 | 1 | 배규태 | 배규태 · 김태현 |
| [T26 · #31](https://github.com/Softbank-Hackathon-Finals/platform/issues/31) | Harbor 평가: 결함 시나리오 반복 (역할 확정 후) | 3 | 배규태 | 배규태 · 전동훈 |
| [T27 · #32](https://github.com/Softbank-Hackathon-Finals/platform/issues/32) | Exit Drill | 1 | 김태현 | 배규태 · 김태현 |
| [T28 · #36](https://github.com/Softbank-Hackathon-Finals/platform/issues/36) | 통합 리허설 · 데모 각본 · reset | 공통 | 전원 공통(Assignee 없음) | 전원 |
| [T29 · #10](https://github.com/Softbank-Hackathon-Finals/platform/issues/10) | 결정 문서 · ADR · 설계 문서 갱신 | 공통 | 전원 공통(Assignee 없음) | 전원 |
| [T30 · #17](https://github.com/Softbank-Hackathon-Finals/platform/issues/17) | 목적지 워크로드: CNPG · PgBouncer · 앱 + Tailscale 연결 | 4 | 김태현 | 김태현 · 류지환 |
| [T31 · #33](https://github.com/Softbank-Hackathon-Finals/platform/issues/33) | 승인 반영 경로: diff → 이미지 → 목적지 배포 | 3 | 전동훈 | 배규태 · 전동훈 |
| [T32 · #34](https://github.com/Softbank-Hackathon-Finals/platform/issues/34) | 이전 대상 추천: 적합성 검사 + AI 추천 | 3 | 전동훈 | 배규태 · 전동훈 |
| [T33 · #53](https://github.com/Softbank-Hackathon-Finals/platform/issues/53) | 보안 탐지 통합: AWS · Google Cloud · 온프렘 탐지를 한 형식 · 한 화면으로 | 2 | 이상무 | 이상무 · 전동훈 |
| [T34 · #56](https://github.com/Softbank-Hackathon-Finals/platform/issues/56) | 감사 로그 수집: 출발지 · 목적지 (DB 접속 · DDL · K8s 권한 변경) | 2 | 이상무 | 이상무 · 전동훈 |
| [T35 · #57](https://github.com/Softbank-Hackathon-Finals/platform/issues/57) | CNPG 백업 · 복원: 출발지 · 목적지 | 2 | 이상무 | 이상무 · 전동훈 |
| [T36 · #103](https://github.com/Softbank-Hackathon-Finals/platform/issues/103) | 판정용 관측 계약: 오류율 · p95 창 요약과 SHIFT · WATCH | 5 | 이상무 | 전원 |
| [T37 · #104](https://github.com/Softbank-Hackathon-Finals/platform/issues/104) | 환경 횡단 지표 수집: OTel Collector → 플랫폼 지표 저장소 | 5 | 이상무 | 전원 |
| [T38 · #150](https://github.com/Softbank-Hackathon-Finals/platform/issues/150) | 구독 런타임 실행 기반: VM·OIDC·격리 Job | 4 | 류지환 | 김태현 · 류지환 |

## 6. 이번 PR에 적용한 결과

| PR | 판단 | 최종 요청 대상 | 처리 |
|---|---|---|---|
| [#74 T1 예산·비용 조회](https://github.com/Softbank-Hackathon-Finals/platform/pull/74) | 파트 4, 작성자 류지환 | 김태현 `kth4778` | 근거 없이 추가했던 배규태의 대기 요청을 제거. 제출된 리뷰 없음 |
| [#73 T29 공통 규칙·칸반 연동](https://github.com/Softbank-Hackathon-Finals/platform/pull/73) | 전원 공통, 작성자 류지환 | 배규태·김태현·전동훈·이상무 | 기존 요청과 일치하므로 중복 요청하지 않음 |

이 표는 최초 요청 정리 기록이다. 이후 리뷰에서 수정 요청이 오면 수정·재검토를 거친다. 최종 커밋의 CI·승인·변경 요청 조건은 [개발 규약](development.md#브랜치와-병합)과 현재 GitHub 규칙으로 확인한다. T1/T29 완료 체크, 이슈 담당, 현재 설계의 역할 배정은 변경하지 않는다.
