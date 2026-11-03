# T38 구독 런타임 · 격리 Job 실측

2026-11-03. 이 증빙은 T38의 무인 실행 기반과 합성 DB_HOST 시험에 한정한다.
T15 두 제품 템플릿, 실제 demo-chat 수정, OpenBao/SeaweedFS 대상 통합, 사람 승인,
T10 영속 저장, T31 이미지 반영을 완료했다고 표시하지 않는다.

## 확인한 결과

| 관측 | 근거 | 결과 |
|---|---|---|
| GitHub OIDC → 고정 SSM → 지정 VM → 실제 구독 모델 2회 | [Actions 41636191012](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41636191012), `oidc-first.json` | 관리자 로그인 없이 PROPOSAL_READY 2회. 임의 shell 명령·Secret 목록 접근 거부 |
| 실제 토큰 회전과 갱신 후 재사용 | [Actions 41642674146](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41642674146), `refresh-and-verification.json` | 첫 호출에서 Secret 버전·access token·refresh token 교체, 유효기간 증가, 동일 subject/client 유지. 두 번째는 갱신된 버전 재사용 |
| 실제 모델 제안 → 독립 Job → receipt | 같은 실행 | 모델 사용량 1,541 / 1,561 tokens. 두 건 모두 환경값 사용·누락 시 차단 PASS, namespace 삭제 확인 후 AWAITING_APPROVAL |
| 의도적으로 잘못된 fallback | `negative-control.json` | 환경값 존재 시험은 PASS, 누락 시험 FAIL. receipt FAIL → RETRY_ALLOWED. namespace 삭제 확인 |
| verifier 배치 불가 | `unavailable-control.json` | 공유 노드 CPU 예약 부족으로 verifier 미실행. UNKNOWN → NEEDS_HUMAN, namespace 삭제 확인. 성공으로 집계하지 않음 |
| 최종 SSM 권한 제한과 정상 경로 | [Actions 41644245414](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41644245414), `final-permissions-and-runs.json` | 임의 shell 명령·기본 shell session·임의 port session·Secret 목록 모두 실제 거부. 정상 모델/Job 실행은 2/2 AWAITING_APPROVAL |
| 실행 종료 정리 | `cleanup.json` | broker 비활성, 자신의 터널 0개, 시험 namespace 0개. 기존 RKE2와 host binding 보존 |

CPU 요청량은 이 작은 HTTP fixture에 맞춰 Pod당 50m에서 10m로 조정했다. CPU limit 500m,
memory limit 256Mi, 비루트·읽기 전용 루트·service-account token 미주입,
네트워크 제한과 별도 verifier Pod는 유지했다. 수정 후 정상/음성 경로가 실제 AWS에서
다시 실행됐다. model/API 인증 값이나 AWS 단기 자격을 공개 증빙에 포함하지 않았다.

최종 controller 실행 SHA는 `cb88b7994ece573d5d971d33c837fcd22ad299e0`이며, 고정 설치된 VM
worker SHA는 `ca71c7354bdda7b3e52137e2c311e59f32a54db2`다. 최종 실행의 모델 사용량은
1,565 / 1,542 tokens였다. [동일 controller SHA의 CI](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41644256906)에서
필수 secrets/infra/platform과 워커 192개 시험을 통과했다. Terraform 경계 시험은 4개다.
AWS 비루트 Python 회귀 시험은 앞선 `f909179` 설치본에서 189개를 통과했으며, 그 수치를
최종 controller의 192개 시험과 혼동하지 않는다.

중간 실행 [41637269329](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41637269329)는
broker 응답 시간 내 결과를 받지 못해 실패했다. 근본 원인은 이 실행에서 특정하지 못했다.
이를 CPU 문제의 해결 증거로 사용하지 않는다. controller가 실패를 반환하고 해당 broker를
종료한 뒤, 후속 실행에서 회전·모델·Job 경로를 다시 검증했다.

## 소스와 판정 경계

`source-manifest.json`은 실제 갱신/Job 실행에 설치한
`ca71c7354bdda7b3e52137e2c311e59f32a54db2`의 소스/lockfile 해시를 고정한다.
최초 OIDC 실행은 `f8d75ce228dcabddf59ad1f31b70e509d33af9ad`다.
proposal의 `111…` source SHA와 `synthetic-ci-example` run ID는 공개 fixture용 식별자다.
실제 demo-chat commit이나 운영 run으로 해석하면 안 된다.

후보 코드는 두 candidate Pod에서 실행되고, 기대값과 검증 코드는 별도 verifier Job에만
주입된다. controller는 source/manifest/proposal 결속을 재검사하고 고정 image를 사용한다.
Job UID 변경·검증 실패·결과 불명·namespace 삭제 실패를 승인 대기로 바꾸지 않는다.
receipt의 PASS는 시험 통과이며 사람이 변경을 승인했다는 뜻이 아니다.

## 작업 분리와 인계

[Slack 합의](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793710551738309)에 따라
이 PR은 T38(구독 VM·OIDC·격리 Job 실행 기반)이다. T15 담당자는 두 제품 템플릿,
T3 namespace lifecycle 연결, 최대 3회 재시도, 정식 diff/digest 승인 결속과 상태 머신 전달을
이어받는다. `verification.py`의 DB_HOST adapter는 기반 연결을 시험하는 예시이며 정식
T15 구현을 대신하지 않는다. 계획/이슈 생성은 담당자의 tasks.md 분리 PR에 따른다.

실제 목적지 준비 증빙은 [T8 PR #131](https://github.com/Softbank-Hackathon-Finals/platform/pull/131)에
별도로 있다. 대상 DB·풀러·SeaweedFS 준비를 전체 앱 startup으로 확대하지 않는다.
T9 OpenBao/ESO credential 전달 경로는 아직 정해지지 않았다. 제안된 소비 계약은 읽기 전용
JSON의 username/password를 기존 300초 TTL로 재조회하는 방식이며, 실제 경로·Secret 이름·키와
회전 시험은 T9/T15/T30 담당자가 확정해야 한다. 실제 값은 모델 입력이나 문서에 넣지 않는다.

T11의 현 파일명 정책이 실제 앱의 `app/db_secret.py`도 차단한다는 연결 전 확인 사항을
T15 담당자에게 인계한다. trusted snapshot의 해당 소스 모듈 허용과 실제 자격 파일 차단을
구분하는 수정·회귀 시험은 제품 템플릿 작업과 함께 검토한다.
