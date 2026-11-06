# T27 실제 목적지 1단계: 잔존 의존성 지도 (2026-11-06)

**실제 `target-dev`에서 [실행 안내](../../operations/t27-exit-drill-real-run.md)의 1단계가 거부 없이 끝났다.** 대조군 Pod를 띄워 계획과 지도를 냈고, 대조군을 UID 조건으로 지운 뒤 Pod · NetworkPolicy 상태가 실행 전과 같았다. 차단 정책은 만들기만 하고 적용하지 않았으므로 판정은 `NOT_RUN`이다. AWS 경로가 막히는지와 앱이 AWS 없이 도는지는 이 기록이 말하지 않는다.

**다만 실행에 쓴 인벤토리는 T16 결함 수정(`65b28b5`) 전 코드로 만들었다.** 같은 입력을 최신 T16으로 다시 만들면 배포에서만 보이는 비밀 항목이 하나 더 나온다. 그래서 같은 스냅샷에 그 인벤토리를 넣어 다시 계산한 계획과 지도를 함께 둔다. 클러스터를 다시 돌린 결과는 아니다.

## 실행

사용자 승인을 받아 11:10 KST에 돌렸다. 코드는 develop `4fad473`(#411 포함)이다.

| 단계 | 결과 |
|---|---|
| 대조군 Pod 만들기 | 서버 검사(`--dry-run=server`)를 통과했다. API 시각으로 생성 1초 뒤 Ready, 실행한 쪽 기록으로 3초 |
| 스냅샷 → `render` | 종료 0, 거부 없음. 차단 정책 `exit-drill-egress-app` · `exit-drill-egress-pooler`를 만들었고 적용하지 않았다 |
| `map` JSON · Markdown | 종료 0 |
| 대조군 지우기 | UID 조건 삭제. 드릴 레이블 자원 0개 |
| 실행 전후 상태 | 26행(Pod · NetworkPolicy의 UID · 재시작 횟수 · 정책 버전)이 같다 |
| 걸린 시간 | 상태 읽기부터 상태 읽기까지 10.8초(파일 시각). 실행한 쪽 기록으로 11초 |

대조군 이미지는 실행 중인 앱 이미지 `localhost/bravo-demo-chat@sha256:b351ba1…`(arm64)다. 안내의 `application.py --arch arm64 --print-image`가 내는 값과 같다. 스냅샷에는 끝나지 않은 Pod 6개가 있었고 명세의 워크로드 여섯(앱 · 풀러 · DB · SeaweedFS · Edge 터널 · 대조군)에 하나씩 맞았다. 끝난 Job Pod 17개는 계획이 세지 않는다.

## 지도

| 구역 | 실행한 지도 | 최신 T16으로 다시 계산 |
|---|---|---|
| 남음 | 5 (cloudflare는 목적지 구현이 Edge 터널) | 5 |
| 불명 | 1 (kms) | **2** (kms, 배포에만 있는 secrets) |
| 유지 (복제 채널) | 2 | 2 |
| 차단 대상 아님 | 1 (SeaweedFS) | 1 |
| 대체 있음 · 아직 확인 안 됨 | 4 (db · iam · object_store · secrets) | 4 |
| 판정 · 후보 적합성 | `NOT_RUN` · `needs_work` | 같음 |

새로 나온 항목은 Deployment가 쓰지 않는 ServiceAccount의 IAM 역할을 따라가 찾은 비밀이다. 위험은 `migration_omission`이고 에이전트 확인을 기다려 불명이다. 두 계획의 차단 정책과 결정은 같다. 인벤토리 해시가 달라 계획 해시만 다르다.

- 실행한 지도: [residual-map.md](residual-map.md) · [JSON](residual-map.json) · [계획](plan.json)
- 다시 계산한 지도: [residual-map-current-t16.md](residual-map-current-t16.md) · [JSON](residual-map-current-t16.json) · [계획](plan-current-t16.json)

## 같은 날 거쳐 온 것

| 시각 (KST) | 무엇 | 결과 |
|---|---|---|
| 03:12 | 대조군 없이 스냅샷을 읽기만 해 `render` | 거부 `SELECTOR_MATCHES_NOTHING`(control) 하나 |
| 10:27 | 1단계 실행 | 거부 `UNCLASSIFIED_POD`. T17이 04:01에 올린 Edge 터널 Pod. #411이 명세에 `edge` 역할로 분류했다 |
| 11:08~11:09 | T30 서비스 계정 반영(#413) | 앱 · 풀러 · SeaweedFS Pod가 새로 떴다 |
| 11:10 | 1단계 실행 | 이 기록 |

10:27 스냅샷을 #411의 명세로 미리 `render`한 계획과 11:10 계획은 해시가 같다. 그 사이 Pod가 바뀌어 스냅샷 해시는 달라도, 계획은 그대로였다.

## 입력

- **인벤토리:** 출발지 Deployment · ServiceAccount · CNPG Cluster 내보내기와 앱 · ESO 역할의 AWS 읽기 결과를 T16 CLI에 넣었다. 출발지 내보내기는 03:53, 인벤토리는 03:55에 만들었다. demo-chat 소스는 목적지 이미지와 같은 `8d82a92`다.
- **T16 코드:** 그때 checkout은 `66a7889`다. 그 커밋으로 다시 만들면 인벤토리 해시 `sha256:182e99f7…`가 그대로 나온다. 최신은 `4fad473`(T16 `65b28b5` 포함)이고 해시는 `sha256:a22a288d…`다.
- **추적 메모:** 두 인벤토리 모두 1줄이다(풀러 Deployment의 default ServiceAccount에 IRSA 주석 없음).

## 공개 처리

| 파일 | 공개 형태 |
|---|---|
| `plan.json` · `residual-map.*` · `control-pod.json` | 원본과 바이트가 같다. ARN · 계정 번호 · UID · 홈 경로가 없다. `public_evidence.py sanitize`도 바꿀 것을 찾지 못해 가림 기록이 없다 |
| `plan-current-t16.json` · `residual-map-current-t16.*` | 위와 같다 |
| `results.json` | 비공개 원본에서 숫자 · 해시 · 판정만 뽑아 새로 썼다. Pod 이름과 UID를 넣지 않았다 |
| 스냅샷 · 실행 전후 상태 · 인벤토리 | 올리지 않는다. Pod 이름 · UID, ARN · 계정 번호가 있다. `results.json`의 `private_originals`에 SHA-256만 둔다 |

## 이 기록이 말하지 않는 것

- **차단(2단계).** 컷오버 전이라 하지 않았다. RKE2 · Canal에서 정책이 거부하는 모양, 실제 Tailscale · AWS 경로가 막히는지는 아직 재지 않았다.
- **확인 안 됨 4건.** 대체 구현이 있다는 T16의 판단이고, 드릴을 통과하기 전에는 확인되지 않은 것으로 둔다.
- **인벤토리의 완전성.** T16이 읽은 입력 안의 결과다. 다시 계산한 지도는 최신 T16 기준이지 그 시각의 클러스터 실행이 아니다.

## 소스

`results.json`의 맨 위 `source_revision` · `source_files`가 실행 코드(exit-drill 패키지 · 명세 · 잠금 파일, 대조군 이미지를 고르는 `application.py` · `app-image.json`)다. `inventory_source_as_run`과 `recomputed_with_current_t16.inventory_source`는 두 T16 커밋의 파일이다. 서명이나 제3자 인증을 뜻하지 않는다. 실행 스크립트는 저장소 밖에 있고, 순서는 안내의 1단계 명령과 같다. 다른 점은 `kubectl create`로 만들어 돌려받은 UID로만 지운 것이다.
