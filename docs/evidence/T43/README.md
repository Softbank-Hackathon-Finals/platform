# T43 플랫폼 GitOps 인수

첫 대상은 T10 등록 API다. [ADR-0028](../../adr/0028-platform-gitops-delivery.md),
[운영 절차](../../../infra/gitops/README.md), [T43 #355](https://github.com/Softbank-Hackathon-Finals/platform/issues/355)를 따른다.
기존 T31 제품 앱 배포와 다른 작업이다.

## 2026-11-06 구현 검증

| 증거 수준 | 확인한 것 | 결과·한계 |
|---|---|---|
| 로컬 이미지 | `28135fd65e92340000bd46f1c3b00d7e2ca6483e`의 등록 API, linux/amd64, SBOM·provenance 포함 | OCI index `sha256:6fcc88db1e8b0f91caea1de96ad5843eaa50c330024ae5f345e1090aa844d9dc`. 이후 checkout 복구 코드의 최종 이미지는 병합 후 Actions에서 다시 빌드·검사해야 한다. |
| 로컬 이미지 검사 | Debian 118개·Python 43개 패키지, HIGH/CRITICAL 중 수정 가능한 취약점 및 시크릿 | 발견 0. 미수정 취약점 전체가 없다는 뜻은 아니다. |
| 로컬 계약 | 이미지·출처·scan 결속, 발행 응답 유실 재조회, 동일 tag 재사용 거부, 잘못된 이벤트 거부, checkout 재시작 | 기존 Executor 이미지 검사 16개, 등록 발행·checkout 검사 8개 통과. |
| 로컬 Helm | 세 자원 유지, Secret 격리, 잘못된 namespace·mutable image·이미지 override 거부, 구성 변경 시 rollout | 운영 설정·토큰 값 없이 검사한다. Argo의 RBAC와 admission 실제 거부는 설치 후 별도로 확인한다. |
| 실제 클러스터 읽기 | 기존 RKE2 한 노드, 4 CPU·약 15.4 GiB allocatable | 당시 요청량 2381m·4030 MiB. Argo 상시 요청 추가 계획 350m·864 MiB. 설치 직전 다시 읽는다. |
| 실제 API dry run | 기존 등록 Deployment·Service·ServiceAccount에 `kubectl apply --dry-run=server` | 세 자원 모두 통과, 변경 0. 기존 UID와 PVC UID는 비공개 실행 기록에 보존했다. 배포 성공을 뜻하지 않는다. |

현재 기록은 GHCR 발행·Argo 동기화·SSO 등록 E2E의 완료 증거가 아니다.
최종 소스 SHA → Actions run/attempt → GHCR digest → values PR → Argo revision →
실제 API·데이터 보존 → drift 교정·Git revert 복구를 동일 실행에 연결한 뒤
해당 결과와 T43 완료 체크를 추가한다. 키·계정·운영 주소·원본 grants는 공개하지 않는다.
