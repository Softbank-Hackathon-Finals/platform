# [ADR-0052] 해피패스에서 GitHub 개입 제거: 플랫폼 후보 빌드와 ECR 자격

* **상태 (Status):** 채택됨(Accepted)
* **날짜 (Date):** 2026-11-08
* **관련 (Related):** T49, T7(후보 발행 영수증), T10 PROPOSE, ADR-0031 workflow host

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** PROPOSE는 GitHub App으로 demo-app에 후보 PR을 만들고 Actions(`publish-candidate-image.yml`)를 실행한 뒤 run·artifact를 읽어 후보 digest를 얻었다.
* **문제:** 실행마다 GitHub 권한·설치·egress가 막혔다. App 설치 범위, artifact 다운로드가 Azure Blob으로 리다이렉트되는 egress(#979·#986), 저장소 이름 변경(#968·#976), deploy key 관리자 권한이 그 예다. 배포는 이미 플랫폼(T31·Argo CD)이 한다.
* **목표:** 사용자 결정(11/8 10:5x): "지금 우리 패스에서 GH가 개입하는 파트 모두 제거해. 배포도 ArgoCD로 하니 말이야." 후보는 플랫폼이 만들고, 신뢰 근거는 플랫폼 빌드 영수증으로 하며, 레지스트리는 ECR로 한다.

## 2. 고려한 옵션들 (Considered Options)

### 1. 플랫폼에서 Dockerfile 전체 재빌드 (Kaniko·BuildKit Job)

**Pros**
* 의존성 파일 변경도 처리한다.

**Cons**
* 플랫폼 노드는 amd64이고 목적지는 arm64라 QEMU 에뮬레이션이 필요하다.
* root 빌드 Pod를 둘 namespace와 빌드 중 후보 코드 실행이 생긴다.

### 2. 등록 기준 이미지 + Proposal 계층 하나 (채택)

등록 때 고정한 다중 아키텍처 기준 index의 각 이미지에 교체 파일만 담은 계층을 덧붙여 push한다.

**Pros**
* 아키텍처와 무관하다. 실행이 없고 결정적이며 수 초 안에 끝난다.
* workflow host가 OCI registry API만 쓴다. blob 다운로드가 없어서 S3 egress가 필요 없다.

**Cons**
* 기준 이미지가 그대로 복사하는 경로(demo-app은 `app/`) 밖의 변경은 표현할 수 없다. 이런 변경은 `candidate_rebuild_required`로 멈춘다.
* 취약점 scan은 기준 이미지 scan과 ECR scan-on-push로 대신한다.

## 3. 결정 (Decision)

* `candidate_build.PlatformCandidateBuilder`가 PROPOSE의 publication을 만든다. `workflow_ports`의 `propose.build`가 필수이며 `github_app`·`selections_dir`·`demo_chat_clone`은 없앴다.
* ECR 자격: `infra/envs/private-registry`가 플랫폼 호스트 역할에 `bravo/demo-chat` 하나에 대한 push 권한을 준다. pull 전용 역할 `bravo-candidate-puller`도 만든다. `infra/envs/candidate-registry/render.py`의 CronJob이 hostNetwork에서 두 토큰을 발급해 `platform-ecr-push`·`platform-ecr-reader`만 갱신한다. IMDSv2 hop 1이라 일반 Pod는 노드 역할을 쓸 수 없다.
* egress: `t10-workflow-host-candidate-registry`(ECR registry 공인 /32, TCP 443)만 연다.
* 기준 이미지 고정: `infra/envs/candidate-registry/pin_base.py`(등록 단계, 운영자 신원).

## 4. 결과 (Consequences)

* PROPOSE에 GitHub 호출·자격·egress가 없다. 실측(11/8): 플랫폼 노드 역할 push 토큰은 업로드 202, pull 토큰은 업로드 403이었다. 다중 아키텍처 후보를 ECR에 push했고, arm64 목적지 노드에서 후보 앱 import가 성공했다.
* 대체가 PASS한 뒤 걷어낼 것: GitHub OIDC publisher 역할, workflow host의 GitHub egress·artifact-storage 정책, App 설치, `publish-candidate-image.yml`. Actions 시절 요청을 복구하는 `legacy_actions_runs` 펜스는 새 계획에서 호출되지 않는다.
* 남은 사람 단계: 목적지 노드 반입(`s2-candidate-import`)은 ECR에서 받는 기존 절차이며 GitHub와 무관하다. 자동화는 별도로 한다.
