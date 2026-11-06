# IAM-01.2의 Prowler 수집 검증

`prowler_iam.py`는 **AWS 앱 역할의 도구 검사 부분**을 검증한다. 이 결과만으로
IAM-01.2 전체를 PASS로 만들지 않는다. kube-bench의 수동 점검과 앱 namespace/RBAC
대응, T5의 승인된 매핑이 여전히 필요하다.

## 지원 범위

- Prowler **5.47.0**, 등록된 ARN과 영구 RoleId가 같은 앱 역할 하나.
- 인라인 정책 하나. 관리형 정책·권한 경계·여러 인라인 정책은 아직 미자격이다.
- 두 점검을 모두 수행한다.
  - `iam_inline_policy_no_administrative_privileges`
  - `iam_inline_policy_allows_privilege_escalation`
- 정책·신뢰 정책의 독립적인 Get/List 조회를 검사 전후 수행한다. 페이지 누락,
  중복, 조회 실패, 역할 재생성, 정책 변경은 통과하지 않는다.
- 스캐너 OCSF에 들어 있는 실제 정책 원문을 별도로 읽은 정책과 대조한다.
- 승인된 기준선은 서버 설정에서 전달한다. 방금 읽은 정책 지문을 자동 승인하지 않는다.
  정책이 달라졌다면 더 좁아졌을 수도 있으므로 위반으로 단정하지 않고 UNKNOWN이다.

두 번의 IAM 읽기는 원자적 스냅샷이나 지속적 권한 보장을 뜻하지 않는다. 검사 창과 승인된
설정 revision을 고정하고, 실행 직전에 다시 검사해야 한다. 이 검사는 자원 기반 정책,
SCP, 세션 정책을 포함한 전체 유효 권한 계산을 대신하지 않는다.

## 종료 코드와 PASS만 믿지 않는 이유

실제 시험에서 Prowler CLI는 역할 ARN 필터를 적용하면 해당 정책 점검을 제외하고
종료 코드 0으로 끝났다. SDK로 두 점검을 명시 실행했을 때는 권한 상승 분석의
`InvalidActionPatternError` 이후에도 PASS 행을 반환했다.

따라서 다음을 모두 확인한다.

1. 필수 점검·대상 쌍이 정확히 한 번씩 존재한다.
2. 도구 버전·범위·실제 수집 정책이 등록/기준선과 맞는다.
3. 전체 수집이 끝났고, stdout/stderr 및 WARNING 이상 로그가 비어 있다.
4. 실제 검사한 정책 원문이 독립 조회와 일치한다.
5. muted/suppressed·미지원 상태를 PASS로 바꾸지 않는다.

오류 문구를 지우거나 실패한 점검을 매핑에서 빼서 통과시키지 않는다. 원문 진단은
비공개로 보관하며 공개 결과에는 고정 사유와 원문 지문만 남긴다.

## 실제 읽기 전용 자격 시험

Prowler는 운영 control-runner 의존성에 추가하지 않는다. 별도 시험 가상환경에
`prowler==5.47.0`을 설치하고 도구 의존성 목록도 함께 기록한다. SDK 자체의 IAM 수집은
계정 수준 목록/보고서 조회를 포함할 수 있다. 앱 역할만 읽는 최소 권한 운영 수집기로
완료됐다고 주장하지 않는다. fixer, Slack, cloud upload, Security Hub 전송은 호출하지 않는다.

### AWS 조회 전 분석기 자격 검사

`prowler_capability.py`는 Prowler를 대체하는 정책 검사기가 아니다. 공식 5.47.0의
분석기 파일 바이트와 `py-iam-expand` 0.3.0을 확인하고, **도구가 제공하는 전체
권한 상승 패턴 집합**을 도구 자신의 확장 함수로 해석할 수 있는지 확인한다.
패턴을 지우거나 무시하는 모드로 바꾸지 않는다. 잘못된 패턴·빈 확장·도구 변경은
`ready: false`이며 provider 오류 문자열은 출력하지 않는다. IAM 데이터 패키지 버전과
실제 패턴·분석기 지문도 남긴다. 이전 ready 결과를 캐시하지 않는다.

실행 도구는 이 검사부터 수행한다. 부적합하면 프로필·등록 정책을 읽거나 AWS SDK를
호출하기 전에 `UNKNOWN / TOOL_ERROR`로 끝난다. `ready: true`라도 기존 실제 정책
수집·대조·진단 검사·대조군 자격을 시도할 수 있다는 의미일 뿐, IAM-01.2 PASS가 아니다.
기존 180초 프로세스 제한을 유지하며 스키마·기준선·필수 점검을 바꾸지 않는다.

이 검사는 신뢰된 운영자가 설치·고정한 별도 도구 환경을 전제로 한다. 분석기 파일
지문 하나로 전체 의존성·설치 환경을 인증하거나 침해된 호스트를 방어했다고 주장하지
않는다. 운영 도구 이미지·의존성 인수는 별도이며 Prowler가 control-runner의 필수
의존성이 되지는 않는다.

신뢰할 수 있는 운영자가 만든 0600 등록 파일:

```json
{
  "role_arn": "<registered role ARN>",
  "role_id": "<registered permanent RoleId>",
  "approved_inventory_sha256": "<previously reviewed baseline digest>"
}
```

```sh
cd services/control-runner
PYTHONPATH=src /path/to/isolated-prowler-venv/bin/python \
  tests/integration/qualify_prowler_iam.py \
  --profile operator-sso \
  --registration /private/path/registration.json \
  --output /private/path/new-qualification-directory
```

부모 프로세스가 180초 제한을 적용한다. 새 출력 디렉터리는 0700, 파일은 0600이다.
원문 정책과 도구 진단은 외부에 게시하지 않는다. 출력의 `full_iam_01_2_qualified`는
항상 false이며, 이 진단용 시험의 완료가 통제 게이트 통과를 뜻하지 않는다.

## 남은 인수

- 현재 Prowler 권한 상승 분석 오류의 해결 및 실패 정책 대조군 재자격.
- 관리형 정책/복수 정책에 대한 완전한 도구 식별자 대응.
- 승인된 정책 기준선과 Kubernetes 앱 권한 점검의 결합.
- 실제 대상 RKE2의 namespace/ServiceAccount/RoleBinding 수집 및 양쪽 매핑 승인.
- 실제 운영 계획에 연결한 신선한 원문과 독립 검증 결과 전달.

실측: [2026-11-06 도구 자격 기록](../../../docs/evidence/t14-prowler-2026-11-06/README.md).
