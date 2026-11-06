# T14 Prowler 실제 앱 역할 자격 시험 — 2026-11-06

## 판정

**IAM-01.2는 UNKNOWN 유지. 실제 AWS 읽기 시험은 수행했지만 도구 자격을 통과하지 못했다.**
운영 정책·역할·앱·DB를 변경하지 않았다. 이 기록은 전체 보안 PASS나 이전 허용 증거가 아니다.

## 시험과 결과

| 항목 | 결과 |
|---|---|
| 도구 | 공식 Prowler 5.47.0 / py-iam-expand 0.3.0 / Python 3.13.18 |
| 실제 대상 | 등록된 출발지 앱 역할 1개, 인라인 정책 1개, 관리형 정책 0개 |
| 독립 수집 | GetRole·ListRolePolicies·ListAttachedRolePolicies·GetRolePolicy, 전후 RoleId/정책 비교 |
| CLI 10:56 KST | 역할 ARN 필터로 두 정책 점검이 실행 집합에서 빠짐. findings 없음, 종료 코드 0 |
| 공식 SDK 명시 실행 | 두 실제 점검 행이 PASS지만 권한 상승 분석 중 InvalidActionPatternError 발생 |
| 수집 검증기 11:03 KST | 도구 오류를 감지하여 `UNKNOWN / TOOL_ERROR`, 전후 정책 동일 |
| 운영 통제 판정 | 승인된 전체 매핑·목적지 시험이 없으므로 PASS로 전환하지 않음 |

실제로 발생한 진단은 `codestar:CreateProject`를 action catalogue에서 찾지 못하는 오류다.
이는 테스트 대상 정책이 해당 권한을 가진다는 뜻이 아니다. Prowler의 권한 상승 패턴 집합을
확장하는 과정에서 발생했으며, 오류를 잡은 뒤 검사 행이 PASS로 생성됐다.

원본 보고서 지문(11:03 실행):
`sha256:0c5cde1232189fae6118c7c8a53eed478ba29a24cb690953be01f4e48dcd789f`.
원본 정책·계정·역할 식별자와 진단 로그는 로컬 비공개로 보존한다. 공개 문서에는
식별자를 제거한 결과만 남겼다. 지문은 서명·승인·출처 인증을 대신하지 않는다.

## 구현한 방어

- 종료 코드 0 또는 PASS 행만으로 도구 실행 성공을 인정하지 않는다.
- 독립 조회한 등록 역할·정책 원문과 스캐너가 검사한 정책 원문을 대조한다.
- 도구 진단이 있거나 정책 수집이 누락되면 UNKNOWN으로 처리한다.
- 역할 재생성, 정책 변경, 기준선 불일치, 중복/누락 점검, suppress 상태를 거부한다.
- 현재 지원 범위를 넘어서는 관리형/복수 정책을 빈 목록으로 처리하지 않는다.

회귀 시험은 합성 정책과 실제 오류 형태를 사용한다. 단위 시험의 PASS를 실제 IAM 통제
PASS로 집계하지 않는다. 자세한 실행 경계는 [수집기 문서](../../../services/control-runner/docs/prowler-iam.md)에 있다.

## 공식 근거

- [Prowler 5.47.0](https://github.com/prowler-cloud/prowler/releases/tag/5.47.0)
- [ARN에 따른 점검 선택](https://github.com/prowler-cloud/prowler/blob/5.47.0/prowler/providers/aws/aws_provider.py)
- [인라인 정책 관리자 권한 검사](https://github.com/prowler-cloud/prowler/blob/5.47.0/prowler/providers/aws/services/iam/iam_inline_policy_no_administrative_privileges/iam_inline_policy_no_administrative_privileges.py)
- [권한 상승 분석과 오류 처리](https://github.com/prowler-cloud/prowler/blob/5.47.0/prowler/providers/aws/services/iam/lib/privilege_escalation.py)
