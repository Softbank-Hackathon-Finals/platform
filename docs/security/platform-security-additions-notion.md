# 플랫폼 보안 거버넌스 추가 구현·참조 정리

> 목적: 기존 **Security Governance v1**과 **Patching as a Service** 문서의 내용을 반복하지 않고,
> 이를 플랫폼에 연결하면서 추가로 구현하거나 참조한 내용을 정리한다.
>
> 현재 상태: **코드·차트·운영 CLI 구현 및 로컬 검증 완료 / 실환경 배포·호출 증적 미완료**

## 1. 한 줄 결론

보안 프로그램을 별도 승인 시스템으로 만들지 않고, 기존 T31 변경 승인과 T10 컷오버 권한의
**추가 fail-closed 조건**으로 연결했다. 보안 서비스는 근거를 판정할 수 있지만 승인·SHIFT·컷오버·
Kubernetes 실행 권한은 갖지 않는다.

## 2. 추가 구현 범위

| 영역 | 추가한 내용 | 결과 |
|---|---|---|
| 변경 승인 | T31 승인·application claim 직전에 `patch/preflight` 강제 | 미등록·FAIL·만료 시 변경 진행 보류 |
| 컷오버 승인 | SHIFT 100% 이후 submit 직전에 `patch/postflight`와 `migration/preflight` 강제 | 둘 중 하나라도 유효하지 않으면 컷오버 보류 |
| 등록 원장 | 기존 `plan_commands`에 보안 등록·철회를 append-only로 기록 | 별도 DB·락·승인 엔진 없음 |
| 동시성 방어 | 원격 review 전후 authority·T19 session·등록 revision 재검증 | 검토 중 계획이나 근거가 바뀌면 거부 |
| 워크로드 신원 | 고정 actor, 전용 projected token, audience와 만료 분리 | 일반 SA token과 다른 짧은 수명 신원 사용 |
| 통신 경계 | 전용 CA, hostname 검증 TLS, ingress/egress NetworkPolicy | 잘못된 CA·origin·통신 경로는 fail closed |
| 공급망 | 외부 `sova-security` 소스를 exact commit으로 고정 | branch/latest 및 작업 디렉터리 오염 배제 |
| 운영 도구 | 등록·철회·실제 enforce probe와 데모 준비 CLI | 승인이나 Executor 호출 없이 사전 검증 가능 |
| 배포 검증 | Helm 값·볼륨·토큰·이미지 import closure·NetworkPolicy 테스트 | 설정 누락을 이미지/차트 단계에서 차단 |

## 3. 기존 플랫폼 권한 경로와의 연결

### 변경 경로

1. 기존 T31 authority가 현재 계획과 승인 조건을 평가한다.
2. 같은 plan transaction에서 현재 T19 session과 `patch/preflight` 등록을 읽는다.
3. 보안 서비스에 `mode=enforce` 원격 review를 요청한다.
4. review가 PASS이고 응답 유효기간이 남아 있는지 확인한다.
5. 기존 authority, T19 session, 등록 revision과 fingerprint를 다시 읽는다.
6. 모든 값이 그대로일 때만 기존 T31 결과를 반환한다.

### 컷오버 경로

동일한 절차로 기존 T10 cutover authority 바깥을 감싸되, 다음 두 근거를 모두 요구한다.

- `patch/postflight`: 실제 apply 결과와 post-apply session에 결속
- `migration/preflight`: 현재 이전 계획·환경·프로필에 결속

보안 review의 `execution_authorized`는 항상 `false`다. 최종 승인은 기존 사람 승인자,
실행은 기존 Executor만 수행한다.

## 4. 등록 원장과 권한 분리

보안 프로그램 등록은 `plan_commands`에 다음 논리 키로 누적한다.

```text
security-program-<kind>-<phase>-<13자리 revision>
```

추가한 권한은 다음 두 개다.

- `security:read`: 현재 등록과 철회 상태를 읽고 review에 사용
- `security:register`: 새 등록 또는 철회를 append-only로 기록

일반 `read`, 변경 승인, 컷오버 승인 권한만으로는 보안 프로그램을 등록할 수 없다.
같은 command key를 다른 입력으로 재사용하거나 예상 revision이 다르면 거부한다.

등록 문서는 다음 값과 정확히 결속된다.

- tenant와 application
- plan ID와 plan digest
- source/target environment
- T19 profile ID와 version
- T19 session fingerprint와 gate phase
- 실제 application ID
- 플랫폼 이미지 source SHA

## 5. 원격 review의 fail-closed 조건

다음 상황에서는 과거 PASS나 기본값으로 진행하지 않고 승인 경로를 보류한다.

- 등록 누락 또는 철회
- stale revision, fingerprint 변경, command key 충돌
- plan·tenant·application·환경·profile·session 불일치
- 플랫폼 이미지 source SHA 불일치
- review 결과 FAIL 또는 필수 control 누락
- review 응답 만료 또는 review 도중 session/registration 변경
- security-review 서비스 장애
- CA·TLS·hostname 검증 실패
- projected token 누락·만료·audience 불일치
- NetworkPolicy로 허용되지 않은 통신

원격 PASS는 캐시해 실행 권한으로 재사용하지 않는다. 실제 승인 시점마다 현재 등록으로 다시 평가한다.

## 6. Kubernetes·워크로드 보안 추가 사항

### 신원과 Secret

- 기본 ServiceAccount token 자동 마운트는 사용하지 않는다.
- security-review 전용 projected token만 `/var/run/secrets/security-review/token`에 마운트한다.
- token audience는 `registered-security-review-audience`, 만료는 600초다.
- T10 intake token과 security-review token은 경로와 audience가 달라 서로 대체할 수 없다.
- TLS root는 `security-review-ca` Secret의 `ca.crt`만 읽는다.
- `insecure-skip-tls-verify` 또는 hostname 검사 해제 경로는 두지 않았다.

### 네트워크

- `t15-rehearsal-host`에서 `security-review:8443`으로 나가는 egress만 추가했다.
- `security-review` ingress는 `t15-rehearsal-host` Pod에서 오는 8443 요청만 허용한다.
- 보안 서비스에는 Executor command credential을 주지 않는다.
- 기존 default-deny와 DNS·DB·Executor·Tailscale 경계는 유지한다.

## 7. 공급망·이미지 무결성 추가 사항

- `sova-security==0.11.1`을 사용한다.
- 소스는 `security` 저장소 commit
  `d7f752b6ef9dec18f198d2e3760a08a1370f71ea`로 고정했다.
- 빌드 시 exact commit을 shallow fetch하고 `FETCH_HEAD`가 기대 SHA와 같은지 확인한다.
- 이미지에는 `src/sova_security` 아래 일반 Git blob만 복사한다.
- Git 메타데이터, symlink, uncommitted 파일, branch 최신 상태는 이미지에 포함하지 않는다.
- build receipt에 `security_revision`을 기록한다.
- approval 이미지 import-closure 검사에 `operator_ports`, `security_relay`,
  `SecurityReviewAuthority`를 포함해 런타임 import 누락을 빌드 시점에 차단한다.

## 8. 운영·데모 준비 CLI

추가된 명령은 `operations/security-program/run.py`에서 제공한다.

```bash
# 변경 승인 전에 patch preflight 등록 + 실제 enforce probe
python operations/security-program/run.py prepare-change \
  --context platform --plan-id PLAN \
  --patch-preflight patch-preflight.json

# 실제 apply와 post-apply T19 평가 뒤 컷오버 근거 등록 + 실제 enforce probe
python operations/security-program/run.py prepare-cutover \
  --context platform --plan-id PLAN \
  --patch-postflight patch-postflight.json \
  --migration-preflight migration-preflight.json
```

두 명령은 다음 순서로 동작한다.

1. `security-review`와 `t15-rehearsal-host` Deployment rollout 준비를 기다린다.
2. 현재 plan revision에 맞춰 등록한다.
3. 실제 TLS·projected token으로 enforce probe를 호출한다.
4. 모두 통과하면 `status: PASS`, 등록 revision, stage별 review 유효기간을 출력한다.

이 준비 명령은 사람 승인, application claim, SHIFT, 컷오버 submit, Executor 호출을 만들지 않는다.
따라서 데모 본 흐름에는 새 수동 승인 단계를 넣지 않고, 시작 전 준비와 기존 승인 순간의 짧은 review만 추가한다.

### 데모 지연과 워밍 인수 기준

- PHP2Shell fixture·회귀 시험·문서 추가는 런타임 경로가 아니므로 지연을 추가하지 않는다.
- 변경 승인에는 patch preflight HTTPS review 1회, 컷오버에는 patch postflight와 migration preflight
  review 2회만 추가된다.
- review는 호출당 전체 2초 deadline이며 retry하지 않는다. 준비된 클러스터에서는 짧게 끝나고,
  장애 시 변경은 약 2초, 컷오버는 약 4초 이내 HOLD가 상한이다.
- `warm`은 `security-review`, `t15-rehearsal-host` 각각 최대 120초 rollout을 기다린다. 이 대기는
  본 시연 중 수행하지 않고 데모 시작 10분 전 준비 단계에서 끝낸다.
- 시작 전 `prepare-change`가 `PASS`가 아니면 데모를 시작하지 않는다.
- `prepare-cutover`는 미리 실행하지 않고 실제 apply와 post-apply T19 session 생성 직후 실행한다.
- 워밍·probe 실패 시 보안 조건을 비활성화하지 않는다. DNS, TLS, projected token, registration을
  복구하고 다시 인수한다.

## 9. 추가 참조한 정부 자료

### 과학기술정보통신부·KISA — 에이전틱 AI 제로트러스트 안내서

- 공식 게시: https://www.kisa.or.kr/2060204/form?page=1&postSeq=25
- 발간일: 2026-11-06
- 적용한 관점: 입력, 도구 호출, 작업 위임, 외부 통신의 지속 검증, 비인간 신원,
  최소 권한, 위험 작업의 인간 승인
- 플랫폼 반영: 고정 workload identity, 요청별 fresh review, 권한 분리, review와 실행 권한 분리

### 행정안전부 — 공공부문 인공지능 윤리기준

- 공식 게시: https://mois.go.kr/frt/bbs/type001/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000015&nttId=129928
- 게시일: 2026-10-01
- 적용한 관점: 안전성, 책임성, 투명성, 프라이버시 보호를 플랫폼 책임 경계에 표시
- 제한: 현재 경로는 생성형 AI 출력이 승인 결정을 내리는 구조가 아니므로 형평성·설명 가능성
  평가는 비대상이다. 개인정보가 review 문서에 들어오는 경우에는 별도 개인정보 영향평가가 필요하다.

### 행정안전부 — 공공부문 AI 도입·활용 가이드

- 공식 게시: https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000008&nttId=126757
- 게시일: 2026-06-10
- 적용한 관점: 기획·구축·운영 전체 수명주기에서 책임과 증적을 유지
- 플랫폼 반영: preflight/postflight 분리, 등록·철회 원장, 배포 전 probe, 운영 인수 체크

위 자료는 외부 통제 기준으로 참조하지만, 현재 구현만으로 법적 적합성·정부 인증·지침 전체 준수를
주장하지 않는다. KISA 원문은 저장소에 복제하거나 수정 배포하지 않고 공식 게시물에 링크한다.

## 10. T36·T19·T24와의 관계

- **T36 오류율·p95 관측 계약:** 기존 SHIFT/WATCH 판정 권한을 유지한다. 보안 review가 오류율이나
  p95를 새로 계산하거나 T36 판정을 대체하지 않는다.
- **T19 gate session:** 보안 등록을 현재 plan/profile/environment/application과 결속하는 기준이다.
  postflight는 실제 apply 이후 생성된 session이어야 한다.
- **T24 감사:** 등록·철회, review fingerprint, 기존 승인과 실행 receipt가 하나의 계획 흐름으로
  연결되는지 실환경 인수 때 확인해야 한다.
- **T31/T10:** 보안 프로그램은 두 authority의 추가 조건일 뿐 새로운 최종 authority가 아니다.

## 11. 검증한 항목

### 단위·통합 테스트

- 변경 승인이 `patch/preflight` PASS를 요구하는지
- 컷오버가 `patch/postflight`와 `migration/preflight`를 모두 요구하는지
- 철회된 등록이 원격 호출 전에 거부되는지
- 다른 플랫폼 source SHA에 묶인 등록이 거부되는지
- 배포된 이미지 SHA와 설정 SHA가 다르면 기동 설정이 거부되는지
- 데모 준비가 rollout 준비 → 등록 → 실제 probe 순서를 지키는지
- rollout 실패 시 어떤 등록도 만들지 않는지
- Helm이 별도 token audience와 CA Secret을 정확히 렌더링하는지
- 외부 보안 package가 exact commit의 committed blob만 포함하는지

최근 로컬 결과:

- 관련 테스트 묶음: `104 passed, 24 deselected`
- 보안 거버넌스·준비 CLI 집중 테스트: `8 passed`
- Helm 관련 테스트: `4 passed`
- 전체 테스트는 로컬 `pg_config` 부재로 의존성 설치 단계에서 중단됐으며, 변경 코드 실패는 아니다.

## 12. 아직 실환경 인수가 아닌 이유

코드와 테스트가 통과해도 다음 실환경 증적이 아직 없으면 운영 PASS로 기록할 수 없다.

- 실제 `security-review` Service와 TLS Secret 배포
- 실제 Service endpoint와 Pod UID 확인
- `t15-rehearsal-host` SA UID와 workload grant 확인
- projected token의 실제 TokenReview와 audience 검증
- NetworkPolicy 정상 경로와 차단 대조군 확인
- 실제 plan으로 `prepare-change`와 `prepare-cutover` PASS 확인
- 등록 누락·철회·잘못된 CA·서비스 중단 시 승인 HOLD와 Executor 0회 확인
- T24 감사에서 등록 → review → 사람 승인 → 실행 receipt 연결 확인

즉, 현재는 **구현 완료**이지 **실환경 인수 완료**가 아니다.

## 13. 실환경 적용 순서

1. `security-review` Service, TLS Secret, workload grant를 `platform-state`에 배포한다.
2. `security-review-ca` Secret과 보호된 `operator.json`을 만든다.
3. caller egress와 service ingress NetworkPolicy를 적용한다.
4. approval host 이미지를 발행하고 동일 source SHA로 pin PR을 병합한다.
5. registrar에게 plan별 `read`, `security:read`, `security:register`를 부여한다.
6. 승인자와 실행자에게 기존 권한과 함께 `security:read`를 부여한다.
7. 실제 plan으로 변경 전 `prepare-change`를 실행한다.
8. 실제 apply와 post-apply T19 평가 후 `prepare-cutover`를 실행한다.
9. 정상·거부 대조군과 T24 감사 연결을 확인한 뒤 운영 인수로 기록한다.

## 14. 주요 구현 파일

| 파일 | 역할 |
|---|---|
| `services/platform-events/src/bravo_platform_events/security_governance.py` | 등록 원장, 결속 검증, 원격 review authority |
| `services/platform-events/src/bravo_platform_events/security_relay.py` | 보호된 등록·철회·probe relay |
| `services/platform-events/src/bravo_platform_events/operator_ports.py` | 기존 T31/T10 authority에 보안 조건 연결 |
| `services/platform-events/src/bravo_platform_events/operator_access.py` | `security:read`, `security:register` 권한 추가 |
| `operations/security-program/run.py` | 운영 등록·철회·probe·데모 준비 CLI |
| `infra/charts/rehearsal-host/templates/deployment.yaml` | CA와 projected token 마운트 |
| `infra/envs/rehearsal-access/rehearsal-host-network.yaml` | security-review 통신 NetworkPolicy |
| `services/platform-events/deploy/product-read/build.py` | security package exact commit pin과 이미지 포함 |
| `services/platform-events/tests/test_security_governance.py` | 권한·등록·fail-closed 통합 테스트 |
| `services/platform-events/tests/test_security_program_run.py` | 데모 준비 순서·실패 테스트 |

## 15. 남은 별도 과제

- review 입력에 개인정보·민감정보가 들어올 가능성이 있으면 분류·보유·파기 정책과 개인정보 영향평가 추가
- 정부 안내서의 세부 점검 항목 번호별 정식 crosswalk 작성
- 실환경 근거 생산자와 security-review 서비스의 운영 주체·장애 대응·rotation 책임 확정
- 정부 문서 개정 시 적용 버전과 잔여 위험을 PR 및 운영 인수 기록에 갱신

## 16. PHP2Shell AI 모의해킹 기사 반영

### 확인한 사건

- MBC는 2026-11-07 허가된 AI 모의해킹에서 구버전 PHP 서버가 17분 42초 만에 장악됐다고 보도했다.
- 기사: https://imnews.imbc.com/replay/2026/nwdesk/article/6863935_37004.html
- 공격 명령과 정확한 대상 build/SAPI가 공개되지 않아 기사 대상의 정확한 exploit은 단정하지 않는다.
- 방어 기준은 PHP 공식 RCE인 `CVE-2026-109618`, `CVE-2026-109619`로 고정했다.

### 추가 구현

- 공식 CVE와 PHP 8.4.26→8.4.27 수정 경계를 담은 비공격성 scanner fixture 추가
- 두 CVE가 확인된 침해를 `EMERGENCY / preserve-rebuild-rotate`로 판정하는 시험 추가
- PHP 버전을 수정하고 finding이 없어져도 compromise state가 확인 상태면 HOLD하는 시험 추가
- PHP2Shell `risk-assessment` FAIL이면 플랫폼의 normal T31 변경 authority가 결과를 반환하지 않는 시험 추가
- 공격 payload, 웹셸, 외부 스캔 기능은 구현하거나 저장하지 않음

### 운영 원칙

1. PHP-FPM, `mod_php`, `php-cgi`, FrankenPHP, LiteSpeed를 모두 inventory 대상으로 본다.
2. PHP 8.2.35, 8.3.36, 8.4.27, 8.5.12 이상 또는 인증된 vendor backport만 수정 근거로 인정한다.
3. 설정 완화나 WAF만으로 PASS를 만들지 않는다.
4. 침해가 확인됐으면 in-place update가 아니라 증거 보존, clean rebuild, 자격증명 회전이 필요하다.
5. 새 image의 전체 scan과 patch postflight가 통과해도 compromise 조사가 닫히지 않으면 컷오버하지 않는다.

상세 계약과 실환경 증적 목록은 `docs/security/php2shell-ai-pentest-incident.md`에 정리했다.
