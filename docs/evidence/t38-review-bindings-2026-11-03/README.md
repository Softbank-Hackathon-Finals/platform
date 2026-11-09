# T38 PR130 제안·실행 결속 수정 검증

검증 소스는 `bc45db2ad2fb011fb18d6b6ba1d485243a1d2816`이다.
이 문서와 `runtime.json`은 그 소스의 검증 기록이며, 운영 배포 완료 기록이 아니다.

## 수정한 경계

- [P1 원문](https://github.com/Softbank-Hackathon-Finals/platform/pull/130#discussion_r4455498861):
  controller가 생성한 run ID마다 broker가 별도 bundle/proposal을 확정한다.
  SSM verify에는 제한된 run ID와 proposal digest를 전달하고 Job 생성 전에 대조한다.
  controller는 receipt의 run/source/proposal/attempt, PASS/effects_known,
  실제 evidence digest와 정리·시험 결과가 모두 일치해야 성공을 기록한다.
- [P2 원문](https://github.com/Softbank-Hackathon-Finals/platform/pull/130#discussion_r4455498879):
  생성 Job UID, 단일 Pod의 controller owner UID/Pod UID, `run` 컨테이너의
  imageID/실행 ID/종료 상태를 확인한다. 고정 Pod·컨테이너의 로그를 읽은 뒤
  candidate와 verifier의 동일성을 다시 확인한다. 불일치는 UNKNOWN이다.

기존 controller, broker, verification 경계에 검사를 모았으며 별도 실행 관리자를 추가하지 않았다.

## 시험 결과

| 검증 | 결과 | 범위 |
|---|---|---|
| macOS 워커 회귀 | 248 PASS | Ruff, 공개 증빙 검사, diff 검사도 통과 |
| AWS 비루트 워커 회귀 | 248 PASS, 8.32초 | 임시 소스 경로에서 위 commit 실행 |
| Terraform | validate, mock 4 PASS, recursive fmt PASS | 실제 IAM/SSM 변경 없음 |
| 실제 RKE2 정상 제안 | PASS / AWAITING_APPROVAL | 독립 verifier exit 0, 두 환경 검사 true |
| 실제 RKE2 localhost 음성대조 | FAIL / RETRY_ALLOWED | 독립 verifier exit 1, 두 환경 검사 false |
| 요청 run ID/digest 불일치 | Job 생성 전 거부 | 실제 클러스터 시험에서도 namespace 생성 없음 |
| [GitHub CI](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41652316927) | 12 jobs SUCCESS | 필수 secrets/infra/platform 포함 |

새 회귀 시험은 누락·다른 실행·이전 실행 receipt, evidence 변조, 다른 Job 소유 Pod,
controller owner 누락, 같은 이름의 Job/Pod 재생성, 복수 Pod, 잘못된 spec/imageID,
컨테이너 재시작·교체·잘못된 종료 상태를 포함한다. 원래 head `e2fa8de3`에서는
이전 receipt 재사용과 다른 Job 소유 Pod가 승인 대기로 수락되는 반례를 재현했다.
수정본은 이를 거부한다. 이 API 응답 변조 시험은 실제 클러스터 공격 실험과 구분한다.

## 정리와 증빙 범위

실제 시험의 namespace 2개를 run ID로 표시하고 원래 UID를 삭제 전제조건으로 사용했다.
삭제 후 부재를 확인했으며 소유 임시 디렉터리도 제거했다. 별도 readback에서도
해당 namespace와 디렉터리가 모두 0개다. 기존 namespace 10개의 UID, broker 설치
경로와 설정 해시를 보존했고 broker는 inactive, RKE2는 active였다. T8 VM은 변경하지 않았다.

`runtime.json`은 원본 영수증을 요약한 공개 자료다. 원본 evidence digest를 별도로
재계산했고, Job/Pod/container/namespace 식별자는 해시로 표시한다. 공개 요약의
digest가 원본 receipt digest와 같다고 주장하지 않는다. 비공개 원본과 정리 readback은
작성 세션 인계에 보존했다.

이번 검증은 결정적 합성 제안과 실제 Job 실행이다. 새 controller↔broker↔고정 SSM
서비스 전체 OIDC 경로의 재배포·재실행, 새 모델 호출·토큰 갱신, T15 제품 템플릿,
T10 승인 영속화·T31 배포는 수행하지 않았다. 운영 전환 시 broker 소스, SSM 문서와
controller의 프로토콜을 함께 반영하고 전체 OIDC 경로를 다시 확인해야 한다.

첫 CI는 새 Terraform 시험 파일의 재귀 포맷 검사에서 실패해 수정했다. 첫 AWS 시험은
비루트 pytest 임시 폴더의 부모 쓰기 권한 문제로 Job 실행 전에 실패했다. 검증 스크립트의
basetemp를 소유 디렉터리 아래 자식 경로로 고친 다음 위 결과를 얻었다.
