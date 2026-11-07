# [ADR-0048] SOVA Deploy: RAILSHOT 배포 엔진을 플랫폼의 별도 파드로 이식한다

* **상태 (Status):** 채택됨(Accepted). 2026-11-08 T21 담당(사용자 결정: "RAILSHOT 엔진 이식", "해당 배포 파드는 별도로 올려").
* **날짜 (Date):** 2026-11-08
* **관련 (Related):** T21 #27, ADR-0040(SOVA MCP), Jasmin-Softbank/Railshot(`ci/`, `apps/api`, `apps/agent`), `infra/envs/private-registry`, `services/platform-events/github_app.py`

## 1. 배경

* SOVA Console에서 "앱을 넣으면 배포"가 필요하다. 입력은 공개 GitHub 저장소·ZIP·폴더 무엇이든 받고, CI 에이전트가 검사·수정한 뒤 등록된 목적지(AWS·온프렘·GCP)에 배포한다.
* 팀의 예선 결과물 RAILSHOT이 이 흐름을 갖고 있다: 소스 준비 → 컨테이너 구성 → L0 정책 · L1 spec · L2 빌드 · L3 실행 검사 → (실패 시) AI 수정·재검사 → 검증한 이미지 그대로 게시 → GitOps 적용 → 공개 HTTP 확인. 품질 기준으로 지정됐다. railshot.io는 현재 내려가 있다.
* 데모 수준 범위(11/8): 각 단계가 동작함과 경계를 보이고, 채점셋 없는 프로덕션 규약을 늘리지 않는다.

## 2. 결정

1. **별도 파드:** `services/sova-deploy`를 플랫폼 RKE2의 독립 Deployment로 둔다. SOVA MCP·T10 호스트와 프로세스·권한·이미지를 나눈다. MCP는 배포 API를 호출하는 도구(`sova_deploy_*`)만 갖는다.
2. **엔진 이식:** RAILSHOT `ci/scripts`의 실행기(intake · native_packaging · gate L0–L3 · repair_evidence · loop · bundle)를 출처를 남겨 옮기고, 단계 계약(`stages.json`)과 결과 기록(`evidence.json`, `gate-N/L*.json`)을 그대로 쓴다. 성공 판정은 검사 결과와 이미지 digest로만 한다(모델의 완료 보고로 판정하지 않음).
3. **CI 에이전트:** 빌드·실행 검사는 플랫폼 노드가 아닌 일회성 GitHub Actions 러너에서 한다(플랫폼에 privileged 빌드 파드를 두지 않음). 배포 파드가 플랫폼 GitHub App 설치 토큰으로 워크플로를 실행·조회한다. AI 수정은 OpenAI Codex(구독)로 하며, 자격이 없으면 `RAILSHOT_MAX_REPAIR_ATTEMPTS=0`으로 결정적 검사만 돈다.
4. **게시·pull:** 검증 이미지는 기존 사설 레지스트리 경로(GitHub OIDC 게시 → 목적지 자격 공급자 pull)를 쓴다. 장기 레지스트리 키를 만들지 않는다.
5. **목적지:** SOVA에 등록된 환경만 고른다. 온프렘 RKE2는 플랫폼의 tailnet egress, AWS EKS는 기존 플랫폼 자격(credentials.acquire 600초)으로 적용한다. 앱별 네임스페이스와 NetworkPolicy를 선언해 만든다.
6. **입력:** 공개 GitHub URL은 바로, ZIP·폴더는 콘솔이 배포 API로 올리면 배포 파드가 앱 저장소 브랜치로 등록한다(RAILSHOT의 apps 저장소 방식).

## 3. 단계와 남은 경계

| 단계 | 내용 | 경계 |
|---|---|---|
| 1 | 배포 API·상태 모델·MCP 도구·콘솔 화면, GitHub URL 입력, 결정적 검사(L0–L3) | GitHub App이 앱 저장소에 actions 권한으로 설치돼 있어야 함(조직 관리자) |
| 2 | 사설 레지스트리 게시, 온프렘 RKE2 적용, 공개 HTTP 확인 | 목적지 pull 자격 공급자 설정 |
| 3 | ZIP·폴더 입력, Codex AI 수정 루프 | 구독 자격의 CI 전달 방식 |
| 4 | AWS EKS·GCP 적용 | 각 목적지 자격 |

배포 접수(202)·이미지 게시·클러스터 적용·URL 확인은 서로 다른 결과로 표시한다.
