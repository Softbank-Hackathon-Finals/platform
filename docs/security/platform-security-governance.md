# Platform security governance enforcement

이 문서는 `security` 저장소의 Security Governance v1과 Patching as a Service 계약을
플랫폼의 기존 T31/T10 승인 경로에 연결하는 배포 계약이다. 보안 서비스는 근거를 판정할 뿐,
승인·SHIFT·컷오버·Kubernetes 변경 권한을 갖지 않는다.

## 정부 지침 참조

다음 자료를 외부 통제 기준으로 참조한다. 이 문서의 적용만으로 법적 적합성, 정부 인증 또는
지침 전체 준수를 주장하지 않는다. 원문 버전과 세부 점검표를 기준으로 별도 적합성 검토가 필요하다.

- 과학기술정보통신부·KISA, [에이전틱 AI 제로트러스트 안내서](https://www.kisa.or.kr/2060204/form?page=1&postSeq=25), 2026-11-06
- 행정안전부, [공공부문 인공지능 윤리기준](https://mois.go.kr/frt/bbs/type001/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000015&nttId=129928), 2026-10-01
- 행정안전부, [공공부문 AI 도입·활용 가이드](https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000008&nttId=126757), 2026-06-10
- 사고 사례·공식 PHP 권고 매핑: [PHP2Shell AI 모의해킹 사례 반영](php2shell-ai-pentest-incident.md)

현재 플랫폼 매핑은 다음과 같다.

| 외부 기준 적용점 | 플랫폼 통제와 증적 | 상태 |
|---|---|---|
| 입력, 도구 호출, 작업 위임, 외부 통신을 신뢰하지 않고 계속 검증 | plan lock 안의 원격 review, nonce·5초 만료, review 후 authority·session·registration 재검증 | 적용 |
| 위험 작업에 사람 승인 또는 사전 승인 정책 적용 | security-review는 `execution_authorized=false`; 기존 사람 T31/T10 승인과 Executor 권한을 대체하지 않음 | 적용 |
| 비인간 신원과 자격증명 수명주기 관리 | 고정 actor, audience 제한 projected token, 600초 만료, request body principal 거부 | 적용 |
| 최소 권한과 통신 경계 | 계획별 `security:read`/`security:register`, 전용 CA·TLS origin, ingress/egress NetworkPolicy | 적용 |
| 기획부터 운영까지 수명주기별 확인과 책임 추적 | preflight/postflight 분리, append-only 등록·철회, source SHA 결속, T24 감사 연결 확인 | 적용 |
| AI 출력의 형평성·설명 가능성·정확성 평가 | 이 경로는 생성형 모델의 출력으로 승인 여부를 결정하지 않는 결정론적 보안 판정 경로임 | 비대상 |
| 개인정보·민감정보의 입력, 보유, 파기와 정보주체 권리 | 현재 계약은 보안 review 문서에 개인정보가 없다는 전제이며 별도 개인정보 영향평가를 대체하지 않음 | 별도 검토 |

PHP2Shell처럼 취약 버전이 실제로 장악된 경우에는 버전 업데이트나 finding 제거만으로 보류를
해제하지 않는다. compromise state가 남아 있으면 `risk-assessment`는 FAIL이고, clean rebuild,
자격증명 회전, 복구 검증과 현재 patch postflight가 끝날 때까지 일반 변경·컷오버 경로를 차단한다.

정부 안내서 원문은 저장소에 복제하거나 수정본으로 재배포하지 않고 위 공식 게시물에 링크한다.
통제 구현을 변경할 때는 원문 항목, 플랫폼 통제, 실행 증적, 잔여 위험을 PR 본문에 함께 기록한다.

## 강제 지점

| 플랫폼 지점 | 추가 필수 판정 | 기존 최종 권한 |
|---|---|---|
| T31 변경 승인·application claim 직전 | `patch/preflight` | 현재 T15 후보, `ChangeApprovalService`, 사람의 `approve_change`/`apply_change` |
| SHIFT 이후 컷오버 승인·submit 직전 | `patch/postflight` + `migration/preflight` | 현재 T19 재평가, SHIFT 100%, `T10GateAuthority`, 사람의 `approve_cutover`/`execute_cutover` |

각 판정은 기존 plan transaction 안에서 다음 순서로 수행한다.

1. 기존 플랫폼 authority를 평가한다.
2. 현재 T19 session과 append-only security registration을 같은 plan lock으로 읽는다.
3. 전용 projected token으로 고정 TLS origin의 `/v1/program-reviews`를 호출한다.
4. 최대 5초의 nonce-bound 응답이 전체 최소 control을 PASS했는지 확인한다.
5. 기존 authority, T19 session, registration과 권한을 다시 읽는다.
6. 하나라도 바뀌거나 만료되면 `authority_unavailable`로 보류한다.

원격 응답의 `execution_authorized`는 항상 `false`다. 과거 PASS를 저장해 실행 권한으로
재사용하지 않으며, 서비스 장애·TLS 오류·토큰 만료·등록 누락은 전부 fail closed다.

## 등록과 철회

등록은 `plan_commands`에 `security-program-<kind>-<phase>-<revision>` 형태로 append-only 저장한다.
별도 DB·락·승인 엔진은 만들지 않는다. `security:register`와 `security:read`는 계획별
`OperatorAccess` grant이고, `read`나 일반 변경 승인만으로 등록할 수 없다.

```sh
python operations/security-program/run.py register \
  --context platform --plan-id PLAN --expected-revision 0 \
  --command-key PLAN-patch-preflight-v1 --document patch-preflight.json

python operations/security-program/run.py revoke \
  --context platform --plan-id PLAN --expected-revision 1 \
  --command-key PLAN-patch-preflight-revoke-v1 --kind patch --phase preflight
```

relay의 actor는 `/rehearsal/operator.json`에 고정하며 요청 body의 principal은 받지 않는다.
`pods/exec` 권한은 이 trusted 관리 경계 전체에 해당한다. 같은 command key의 입력 변경,
stale revision, 다른 plan/profile/environment/application/source SHA 등록은 거부한다.

## 승인 호스트 설정

`operator.json`의 `security_review`는 필수다.

```json
{
  "security_review": {
    "url": "https://security-review.platform-state.svc:8443",
    "ca_file": "/security-review/ca.crt",
    "token_file": "/var/run/secrets/security-review/token",
    "subject_id": "platform-approval-host",
    "source_revision": "40자리 플랫폼 이미지 소스 SHA",
    "registrar": {"tenant_id": "등록 tenant", "subject_id": "계획별 registrar grant subject"}
  }
}
```

차트는 기본 ServiceAccount token을 계속 끄고, audience
`registered-security-review-audience`, 만료 600초인 token만 별도 projection한다.
CA Secret은 `secrets.securityReview`의 `ca.crt`만 읽는다. hostname 검사를 끄거나
`insecure-skip-tls-verify`를 쓰는 설정은 없다.

`source_revision`은 차트가 주입한 `BRAVO_PLATFORM_SOURCE_REVISION`과 정확히 같아야 한다.
보안 패키지는 `security` 저장소의
`d7f752b6ef9dec18f198d2e3760a08a1370f71ea`로 잠그며, 이미지 빌더가 해당 commit의
일반 Git blob만 복사한다. branch나 latest package를 런타임에 받지 않는다.

## 배포 순서와 인수

1. security-review Service, TLS Secret, workload grant를 `platform-state`에 배포한다.
2. `security-review-ca` Secret과 보호된 `operator.json`을 만든다.
3. `rehearsal-host-network.yaml`의 caller egress와 service ingress를 적용한다.
4. 승인 호스트 이미지를 발행하고 같은 source SHA의 pin PR을 병합한다.
5. registrar에게 계획별 `read`, `security:read`, `security:register`를 부여한다.
   변경·컷오버 승인자와 실행자에게도 기존 권한에 더해 `security:read`를 부여한다.
6. patch preflight를 등록한 뒤 T31 승인의 정상/거부 대조군을 확인한다.
7. 실제 apply 뒤 post-apply T19 session에 patch postflight와 migration preflight를 등록한다.
8. 등록 누락·철회·잘못된 CA·잘못된 audience·서비스 중단 각각에서 승인과 submit이
   `authority_unavailable`이고 Executor/Kubernetes 외부 호출이 0회인지 확인한다.

코드·차트가 병합됐다는 사실은 실환경 PASS가 아니다. 현재 Pod UID, Service endpoint,
SA UID/workload grant, 실제 TLS와 TokenReview, NetworkPolicy 정상·거부 대조군, T24 감사
연결을 같은 시간창에서 확인한 뒤에만 운영 인수로 기록한다.

## 데모 직전 준비

세 등록을 최초 session에 한꺼번에 넣으면 안 된다. `patch/postflight`는 실제 application ID와
post-apply T19 session에 결속해야 하므로, 준비를 두 단계로 나눈다. 두 명령은
`security-review`와 `t15-rehearsal-host` rollout 준비를 먼저 기다리고, 등록 후 실제 TLS·projected
token으로 enforce review를 호출한다. 승인·application claim·Executor 호출은 만들지 않는다.

PHP2Shell fixture와 회귀 시험은 배포 런타임 경로에 들어가지 않아 데모 지연을 추가하지 않는다.
런타임에 추가되는 것은 변경 승인 시 patch preflight review 1회, 컷오버 시 patch postflight와
migration preflight review 2회뿐이다. 각 HTTPS review는 전체 요청 기준 2초 deadline이고 retry하지
않으므로, 서비스가 준비된 경우 체감 지연은 짧고 장애 시에는 각각 최대 약 2초·4초 안에 HOLD한다.
`warm`은 두 Deployment마다 최대 120초를 기다리므로 본 시연 중이 아니라 시작 전에 수행한다.

```sh
# 리허설 완료 후, 변경 승인 전에
python operations/security-program/run.py prepare-change \
  --context platform --plan-id PLAN \
  --patch-preflight patch-preflight.json

# 실제 T31 apply와 post-apply T19 평가 후, SHIFT/cutover 전에
python operations/security-program/run.py prepare-cutover \
  --context platform --plan-id PLAN \
  --patch-postflight patch-postflight.json \
  --migration-preflight migration-preflight.json
```

정상 출력은 `status: PASS`, 두 Deployment의 `warmed`, registration revision, stage별 `probe.reviews`를
포함한다. HOLD나 FAIL이면 데모를 시작하지 않는다. probe PASS는 실행 승인이 아니며 저장하거나
재사용하지 않는다. 실제 변경·컷오버 시 같은 review가 plan lock 안에서 다시 수행된다.

인수자는 데모 시작 10분 전 두 Deployment rollout과 `prepare-change` PASS를 확인한다.
`prepare-cutover`는 미리 실행하지 않고 실제 T31 apply 및 post-apply T19 session 생성 직후 실행한다.
DNS·TLS·projected token·registration 중 하나라도 실패하면 보안 조건을 끄지 말고 데모를 HOLD한 뒤
원인을 복구한다. 120초 rollout 대기는 준비 실패 신호이지 정상 데모 시간에 포함할 단계가 아니다.
