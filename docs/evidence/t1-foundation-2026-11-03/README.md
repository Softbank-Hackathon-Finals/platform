# T1 네트워크·공유 상태 검증 — 2026-11-03

팀 `platform` 구현을 사용자 AWS 계정에서 검증했다. 개인 B-free 실측을 재사용하지 않았다. 구현 commit과 파일 SHA-256은 [`source-sha256.json`](source-sha256.json)에 있다. 공개 readback은 계정 번호·리소스 ID·로그그룹 이름을 가린 사본이며, 원본 readback과 실행 로그는 접근을 제한한 비공개 경로에 보존했다. [가림 manifest](../privacy/redaction-manifest.json)는 원본과 공개본의 SHA-256을 구분한다. 기존 소스 해시는 실측 당시 입력을 가리키며 변경하지 않았다. [공개 증빙 절차](../../public-evidence-policy.md)를 따른다.

| 검증 | 결과 | 증거 수준 |
|---|---|---|
| Terraform 1.16.5 fmt·validate | 두 root 통과 | 로컬 |
| 네트워크 경계 및 잘못된 계정·AZ·CIDR 입력 | 6개 통과 | mock provider |
| Ruff, actionlint 1.7.7, gitleaks 8.24.3 전체 이력, Trivy HIGH/CRITICAL | 통과 | 로컬 |
| VPC·피어링 등 32개 생성 → 삭제 | 로컬 상태와 S3 상태에서 각각 1회 완료 | 실제 AWS API |
| 본인용 `bravo-foundation` 생성 | 32개 생성, 설정 대조 25개 통과 | 실제 AWS API |
| 본인용 기반의 재계획 | 변경 없음, 종료 코드 0 | 실제 AWS refresh |
| 상태 저장소 7개 구성 | 버전 관리·KMS·공개 차단·암호화된 bootstrap state 확인 | 실제 AWS API |
| 동시 state 접근 | 첫 apply가 잠금을 보유할 때 두 번째 plan 거부. 첫 apply는 승인 없이 취소, 잠금 해제 확인 | 실제 S3/Terraform |

초기 생성·삭제 두 차례 뒤 Flow Logs의 CloudWatch 조회 권한을 보완했다. 최종 commit으로 본인용 기반을 적용하고 25개 readback 및 변경 없음까지 다시 확인했다. 삭제 검증은 동일한 32개 자원 구조에 대한 기록이며, 최종 본인용 기반은 다음 작업을 위해 유지했다.

확인한 경계: 공인 IP 자동 할당 없음, 기본 SG 양방향 닫힘, 두 VPC의 피어 경로만 존재, 플랫폼에서 출발지 SG로 TCP 443·5432만 신규 연결 허용, 태그·로그 구성 유지. [`aws-readback.json`](aws-readback.json), [`state-lock.json`](state-lock.json), [`backend-readback.json`](backend-readback.json), [`validation.json`](validation.json)을 참조한다.

이 검증은 네트워크 **설정**의 실제 생성·조회·삭제다. 패킷 전달, Flow Logs 이벤트 수신, EKS/RKE2, 앱, GCP 통합은 검증하지 않았다. T6·T7에서 egress와 클러스터 경로를 붙인 뒤 해당 검증을 수행해야 한다.

T1 중 VPC·피어링 항목의 증거다. `component` 태그는 적용했지만 예산 알림·태그별 비용 조회는 아직 남았다. 팀원별 계정/권한 배포는 사용자 지시로 회의 뒤 결정한다. T1 이슈 전체 완료나 팀원 로그인 완료를 뜻하지 않는다.

## 팀 모듈 구조로 정렬한 추가 검증

`infra/modules/network/aws`와 `infra/envs/aws-foundation`, `infra/bootstrap/state-backend`로 경로와 책임을 나눴다. 구현 및 파일 해시는 [`module-source-sha256.json`](module-source-sha256.json)에 기록했다. 앞선 생성·삭제 증거와 그 소스 해시는 변경하지 않았다.

기존 본인용 state의 32개 자원 주소를 `moved` 블록으로 연결했다. 실제 plan은 생성 0·변경 0·삭제 0이었고, 적용 뒤 AWS readback 25개 통과 및 drift plan 종료 코드 0을 확인했다. Terraform 1.16.5로 두 root와 모듈 validate, 모듈 경계 시험 6개, Ruff·Trivy HIGH/CRITICAL도 통과했다. [`module-migration.json`](module-migration.json), [`module-aws-readback.json`](module-aws-readback.json)을 참조한다.
