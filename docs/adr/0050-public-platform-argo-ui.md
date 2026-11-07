# [ADR-0050] 플랫폼 Argo CD 화면을 로그인 없이 공개한다(읽기 전용, 위험 API는 엣지에서 차단)

* **상태 (Status):** 채택됨(Accepted)
* **날짜 (Date):** 2026-11-08
* **관련 (Related):** T43(#355), ADR-0029(플랫폼 GitOps 전달), #743(공개 입구 조정기), #776(`role:platform-viewer`), #797

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 플랫폼 Argo 화면 `platform-argocd.mangowhois.ai`는 공개 입구(#743)를 거쳐 Cloudflare Access(팀 이메일 OTP) 뒤에서 열렸다. 익명 Argo 사용자는 `role:platform-viewer`(Application · 프로젝트 `get`)다.
* **문제:** 사용자 결정(11/8): 데모에서 로그인 없이 화면을 보여 준다. Access를 빼면 Argo의 `applications, get`이 인터넷의 누구에게나 열린다. 이 권한은 화면 조회보다 넓다.
  - 노출되는 정보: Application 값 · 라이브 매니페스트(registration hostAliases의 사설 EKS 엔드포인트 · IP), orphan 트리(관리하지 않는 ConfigMap 이름 · 내용)
  - 서버가 대신 실행하는 일: `ServerSideDiff`(서버 자격 dry-run apply), `GetManifests?revision=` · `manifestsWithFiles`(repo-server 임의 렌더링)
* **목표:** 로그인 없이 동기화 상태를 보여 주되, 운영 식별자와 서버 측 실행은 인터넷에 열지 않는다. T43의 "사람 SSO" 경계를 이 호스트에서만 바꾼다.

## 2. 고려한 옵션들 (Considered Options)

### 1. Access 유지

**Pros**
* 경계가 바뀌지 않는다.

**Cons**
* 사용자 결정(로그인 없는 화면)을 따르지 못한다.

### 2. 공개 + 화면 범위 축소 + 위험 API 엣지 차단 (선택)

**Pros**
* 모두 Git 선언이고 조정기 · Argo가 적용한다. 되돌리기는 `entries.json`의 `public` 한 줄이다(다음 주기에 Access가 다시 생긴다).

**Cons**
* 속도 제한이 없다(아래 남는 위험).

### 3. Cloudflare WAF 허용 목록 + 속도 제한

**Pros**
* 메서드 · 쿼리 단위로 막고 속도도 제한한다.

**Cons**
* 공개 입구 토큰에 zone 규칙 권한을 더해야 한다. 지금 seed한 토큰 범위(Tunnel · Access · DNS) 밖이다.

## 3. 결정 사항 (Decision Outcome)

* **2안을 택한다.**
  - `entries.json`: `platform-argocd`에 `public: true`와 `deny_paths`. Tunnel 엣지가 다음 경로에 403을 돌려준다. `/api/v1/applications/{manifestsWithFiles | */server-side-diff | */manifests | */revisions/…}`, gRPC · gRPC-web 경로 전부(`^/<pkg>.<Name>Service/`).
  - 익명 RBAC: `platform-registration/*` · `platform-rehearsal-host/*`의 `applications, get`을 deny한다. 값에 hostAliases(사설 엔드포인트 · IP, STS IP)가 있다.
  - 플랫폼 AppProject에서 `orphanedResources`를 지운다. 필드가 있으면(`warn: false`여도) orphan 감시가 켜져 관리하지 않는 객체가 트리에 나온다.
  - 쓰기 계정 · 로그 · exec는 그대로 없다. Secret은 추적 종류가 아니어서 화면에 들어오지 않는다.
* **남는 위험(받아들임):**
  - 속도 제한이 없다. `GET /api/v1/applications/{app}?refresh=hard`(조회 권한) 반복으로 repo-server(CPU 1)에 부하를 줄 수 있다. 업무 경로와 무관한 플랫폼 GitOps 지연이다. 3안이 후속이다.
  - 공개 Application 4개의 라이브 매니페스트와 클러스터 안 Service 이름이 보인다.
* **되돌리는 조건:** 데모 뒤, 또는 위 위험이 실제로 관측되면 `public`을 지운다. 지우면 다음 조정 주기(60초)에 Access 앱 · 정책이 다시 생긴다.
