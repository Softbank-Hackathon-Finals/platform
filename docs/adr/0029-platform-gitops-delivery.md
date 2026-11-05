# [ADR-0029] 플랫폼 등록 API를 Argo CD와 Helm으로 배포

* **상태 (Status):** 수락됨(Accepted). 류지환의 2026-11-06 구현 지시와 비공개 GHCR 직접 pull 선택에 따른다. 실제 설치·검증 결과는 T43의 실행 증거로 별도 관리한다.
* **날짜 (Date):** 2026-11-06
* **관련 (Related):** T43, T7 #12, T10 #19. 기존 T31 #33의 제품 배포 계약은 ADR-0021을 유지한다.

## 1. 문제와 범위

T10의 프로필 결속 코드 #328과 T19 소비 코드 #337이 develop에 병합됐지만, 등록 API는 이전에 반입한 고정 이미지와 설정으로 실행되고 있었다. 현재 CI는 병합 검사를 수행하며 등록 API의 지속 배포를 담당하지 않는다. 사람의 세션 인계가 다음 이미지·설정 반영을 시작하는 조건으로 남아 있다.

T7은 플랫폼·OIDC·이미지 발행과 배포 1회가 완료 기준이다. T31은 리허설·승인을 통과한 **이전 대상 앱**의 digest 배포다. 플랫폼 **자체 서비스**의 반복 배포는 새 T43으로 추적한다. 공식 실행 담당은 류지환이며, 인프라 파트 리뷰는 김태현에게 요청한다. T10 등록 계약 변경은 기존 T10 담당 범위로 따로 검토한다.

첫 인수 대상은 등록 API 하나다. DB·PVC, 출발지·목적지 앱, 리허설 namespace, Executor 제어 로직을 함께 인수하지 않는다. 다른 서비스의 확대는 첫 실제 배포·복구 결과 뒤에 별도 범위로 정한다. 현재 T19에 필요한 새 계획 등록의 완료 기준을 T43 전체 완료에 종속시키지 않는다.

## 2. 선택

**기존 GitFlow와 GitHub Merge Queue를 유지하고, Helm으로 선언한 배포 상태를 Argo CD가 적용한다.** CI는 이미지를 빌드·검사·발행하고 배포 설정 PR을 만든다. 실제 클러스터 반영은 Argo CD 한 주체가 맡는다.

기존 수동 이미지 반입은 최초 인수와 비상 복구의 제한된 절차로 남긴다. CI에서 매번 광범위한 kubeconfig로 kubectl을 실행하는 경로, 자체 배포 조정기, 모든 서비스를 감싼 범용 chart는 도입하지 않는다.

## 3. GitFlow와 배포 흐름

| 입력 | Git 절차 | 배포할 상태 |
|---|---|---|
| 기능 수정 | develop에서 `feature/T번호-설명`, develop 대상 PR, 필수 CI와 Merge Queue | 변경 서비스의 검증된 이미지를 발행하고 개발 환경 values의 digest 변경 PR을 만든다. 해당 PR 병합 뒤 개발 Application이 동기화한다. |
| 릴리스 | develop에서 `release/vX.Y.Z`, main PR과 기존 승인·CI, 버전 태그, develop 역반영 | 검증한 동일 이미지 digest와 설정을 승격한다. 릴리스 중 코드가 바뀌면 그 코드의 이미지와 검증 영수증을 새로 만든다. |
| 긴급 수정 | main에서 `hotfix/T번호-설명`, main 반영 후 develop 또는 진행 중 release로 반영 | 수정한 산출물을 검증한 뒤 정확한 digest를 승격한다. |
| 복구 | 이전 검증 digest·설정으로 돌아가는 보호된 Git revert PR | Argo CD가 이전 상태로 수렴한 뒤 API와 데이터 보존을 확인한다. |

개발과 릴리스는 독립된 Application·배포 대상 또는 명시적으로 하나를 선택한 대상에 연결한다. 같은 Deployment를 develop과 main의 두 Application이 동시에 제어하지 않는다. 브랜치만 나눴다는 이유로 개발·운영 격리가 됐다고 표시하지 않는다.

소스 수정 PR과 digest 승격 PR의 역할을 구분한다. values만 바뀐 PR은 서비스 이미지를 다시 빌드하지 않는다. 이전 소스의 늦은 빌드가 새 배포 설정을 덮지 않도록 source revision·기대 이전 digest를 대조한다. 실패하거나 응답이 불명확하면 기존 실행·PR·Argo 상태를 먼저 읽는다.

## 4. 구성과 권한

- 저장소 `platform` 안에서 등록 API chart, 환경별 비밀 없는 values, Argo Application·AppProject를 관리한다. chart·Helm·Argo CD 버전은 구현 시 호환성을 확인하고 고정한다. Argo CD는 Helm template 결과를 적용하며 같은 자원을 별도 Helm release 명령으로 다시 제어하지 않는다.
- chart의 `values.schema.json`에서 이미지 digest와 필수 참조를 검증한다. DB·TLS·collector 자격은 기존 플랫폼 소유 Secret 참조로 받는다. 프로필 허용 정책과 설정 변경은 rollout 및 검증 영수증에 결속한다.
- 공개 저장소에는 키·토큰·개인정보·실제 계정 및 운영 식별자를 넣지 않는다. 실제 환경 값은 보호된 설정·Secret 경로에서 주입한다. OpenBao·ESO를 새로 연결해야 하는 부분은 T9 소유와 합의한다.
- AWS 발행은 기존 OIDC를 사용한다. GitHub의 배포 설정 PR 작성은 기존 제한된 자동화 인증을 재사용하며, AWS OIDC가 GitHub 쓰기 권한까지 주는 것으로 취급하지 않는다. 보호 브랜치 직접 쓰기·개인 장기 키·CI의 광범위한 클러스터 자격을 추가하지 않는다.
- 플랫폼 이미지는 비공개 GHCR에 직접 발행한다. Actions의 단기 `GITHUB_TOKEN`은 발행에만 쓰고, 클러스터는 별도의 만료 있는 `read:packages` classic PAT를 플랫폼 Secret으로 받아 pull한다. 이 자격은 AWS OIDC나 저장소 단위 fine-grained 권한이 아니며, 계정이 읽을 수 있는 package 범위다. 만료 전에 회전하고, 값은 Git·Helm 값·SSM 명령·로그에 넣지 않는다. ECR 미러와 공개 전환은 이 결정에 포함하지 않는다.
- Argo CD의 Git 읽기·클러스터 권한은 플랫폼에서 소유한다. AppProject의 저장소·대상 namespace·리소스 허용 목록과 Kubernetes RBAC를 함께 제한한다. 설치 bootstrap의 일시적인 권한과 Application의 상시 권한을 구분한다.
- 기존 실제 클러스터의 CPU·메모리·저장소 여유와 비용 한도 안에서 설치 크기를 정한다. 별도 유료 관리형 서비스나 새로운 클러스터를 전제로 하지 않는다. 요청·제한 자원량, 실제 증가량과 비용 영향을 기록한다.

## 5. 기존 자원 인수와 운영

먼저 기존 Deployment·Service·정책·ConfigMap의 UID와 이미지·설정·트래픽 경로를 읽고 chart 렌더 결과와 비교한다. 기존 렌더러·수동 절차 중 해당 자원을 쓰는 경로를 중단한 뒤 Argo CD의 단일 관리 대상으로 인수한다. 최초 sync는 차이를 검토한 제한된 창에서 수행한다.

DB·PVC·계획 데이터는 Application 관리 범위에서 제외한다. 초기 인수 때 자동 prune을 켜지 않는다. 이후 삭제가 필요한 stateless 자원의 범위와 복구 시험을 확인한 뒤에만 제한적으로 사용한다. self-heal은 인수한 자원에 적용하며, 기존 실행기의 일시적 제어와 충돌하는 필드는 사전 합의 없이 관리하지 않는다.

외부 인증·collector 토큰의 만료는 Git 동기화만으로 해결되지 않는다. 기존 발급·회전 주체가 유효한 Secret을 공급하고, 등록 API가 이를 실제로 소비하는지 확인한다. 만료된 자격과 권한 거부는 완료로 보고하지 않는다.

## 6. 완료 증거

다음 자료를 한 배포 실행에 연결한다.

1. 소스 SHA, CI run·attempt, 검사 결과, 발행한 이미지 digest.
2. Helm chart·values revision과 비밀값 없는 설정 해시, 보호 PR 병합 결과.
3. Argo CD가 실제 적용한 revision·sync·health 결과와 대상 Deployment UID·Pod imageID.
4. 실제 SSO 등록 API 정상 경로와 잘못된 신원·미허용 프로필의 거부, 기존 등록·계획 보존.
5. 소유 자원의 의도적 drift 교정, 이전 검증 revision으로 Git revert 복구, 시험 자원 정리와 비용 영향.

로컬 렌더·검사, CI, 실제 배포, 제품 API 검증을 각각 기록한다. `Synced` 또는 Pod `Ready`만으로 전체 완료를 판단하지 않는다. 이 계획 PR은 위 운영 시험의 성공을 주장하지 않는다.

## 7. 근거

- [팀 GitFlow 및 병합 규약](../development.md)
- [T7 현재 완료 기준](https://github.com/Softbank-Hackathon-Finals/platform/issues/12), [T31의 별도 제품 배포 범위](https://github.com/Softbank-Hackathon-Finals/platform/issues/33)
- [등록 계획 프로필 결속 #328](https://github.com/Softbank-Hackathon-Finals/platform/pull/328), [T19 소비 대조 #337](https://github.com/Softbank-Hackathon-Finals/platform/pull/337)
- [Argo CD의 Helm 사용](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/), [자동 sync·self-heal·prune](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/)
- [Helm chart와 values schema](https://helm.sh/docs/topics/charts/)
