# T1 팀 접근 사전 준비

상태: 읽기 전용 사전 조회와 사람용 개발자 IAM 준비 도구. 11/3 후속 사용자 지시로 회의 대기보다 IAM 사전 준비를 우선하며, 사람용 개인 주체에 개발용 AdministratorAccess를 적용한다. 이 권한 확대는 제품 AI 워커·CI 역할에 적용하지 않는다. EKS/RKE2 실제 접속과 개인 로그인 검증은 별도다. 아래의 최초 준비 조회는 당시 상태이며, 적용 결과는 [개발자 접근 운영 안내](human-developer-access.md)와 최신 실행 증거를 본다.

11/3 [접근 요청과 답변 스레드](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793688724749199)는 개인 SSO, 출발지 EKS 접근, 플랫폼 RKE2 경로·권한별 kubeconfig를 요구한다. 당시에는 초대·배포를 운영자 검증 뒤 회의에서 확정한다는 답변이 있었고, 이후 사용자가 IAM을 먼저 준비하도록 명시했다. 이후 11/4 사용자는 기존 계정 하나의 서울 단일 리전 조직형 SSO와 운영자 검증 후 요청한 팀원 초대를 승인했다. 현재 생성·할당과 미완료 개인 로그인은 [운영자 SSO 검증](operator-sso-verification.md)에 구분한다. 초대 수락·실제 본인 접속을 확인하기 전에는 접근 인수 완료로 표시하지 않는다. 팀 정본은 현재 **한 AWS 계정 안의 출발지/플랫폼 VPC 분리**다(`design.html` 7.2). 요청의 “앱·플랫폼 계정”이라는 표현만으로 새 AWS 계정 둘을 만들지 않는다.

## 접근 계약

| 주체 / 목적 | 준비할 계약 | 확인할 것 |
|---|---|---|
| 사람 | 개별 SSO 사용자·그룹, 대상 계정 permission set, 세션 만료, 해제 담당자 | 본인 SSO 로그인과 승인된 역할/계정 대조, 잘못된 자격·미할당 접근·만료/해제 시험 |
| T6 EKS | IAM 역할 ARN의 STANDARD access entry, 작업 namespace 범위 RBAC 또는 access policy, endpoint 도달 경로 | 개인 로그인으로 실제 허용 작업 확인. 제한 역할은 namespace·Secret·관리 작업 거부를 확인하고, 승인된 개발 관리자는 무자격·잘못된 자격·권한 해제 대조군을 적용 |
| T7 RKE2 | 개별 신원 인증 방식, endpoint·CA 검증, 역할별 RBAC, 만료/폐기 절차 | 개인 kubeconfig로 허용·거부 시험, 만료 또는 해제 뒤 재접속 거부 |
| 수집기 | 읽기 전용 IAM/서비스 계정 | 원본 읽기는 성공, 쓰기는 거부 |
| Executor | 명시된 DB/PgBouncer 제어와 클러스터 동작만 허용 | T12/T15의 실제 제어·시험 동작, 업무 데이터/임의 관리자 동작의 별도 경계 |
| AI 워커 | 운영 자격 없음, 도구 계층의 리허설 namespace 권한만 사용 | 운영 kubeconfig·DB 자격 접근 거부 |
| GitHub Actions | OIDC와 배포 역할 | 사람 로그인·AI 워커 권한과 분리 |

사람의 개발/운영 접근과 제품의 에이전트 권한은 다른 경계다. 이 표는 실제 IAM 정책이나 RBAC에 이미 반영된 상태를 뜻하지 않는다. 이름·역할·namespace와 필요한 동작은 배포 전에 각 소비자와 확정한다. 후속 사용자 승인은 확인된 개발 운영자의 사람용 AdministratorAccess와 개인 EKS 관리자 접근 준비에 적용한다. 해당 permission set에 할당된 모든 사용자가 같은 역할 권한을 받으므로 실제 할당 명단을 함께 검토한다. 이를 미승인 팀원 일괄 초대나 CI·노드·제품 AI 워커 권한 확대로 해석하지 않는다. Executor의 제어 권한도 AI 워커에 넘기지 않는다.

AWS 계정 permission set에는 조직 인스턴스가 필요하다. 계정 인스턴스는 AWS 계정 접근용 permission set을 지원하지 않는다. 실제 Identity Center 리전을 운영자가 확인한 뒤 조회한다. 특정 리전의 0개 결과는 다른 리전의 부재를 증명하지 않는다. [AWS permission sets](https://docs.aws.amazon.com/singlesignon/latest/userguide/permissionsets.html), [계정 인스턴스 제한](https://docs.aws.amazon.com/singlesignon/latest/userguide/account-instances-identity-center.html), [리전별 데이터와 작업](https://docs.aws.amazon.com/singlesignon/latest/userguide/regions.html).

EKS access entry에는 IAM 역할 ARN을 쓰며 일시적인 STS 세션 ARN을 쓰지 않는다. 역할의 access entry가 존재해도 namespace 권한·endpoint 경로·DB 권한까지 보장하지 않는다. [CreateAccessEntry API](https://docs.aws.amazon.com/eks/latest/APIReference/API_CreateAccessEntry.html), [EKS 인증 모드](https://docs.aws.amazon.com/eks/latest/userguide/setting-up-access-entries.html).

RKE2의 `/etc/rancher/rke2/rke2.yaml`은 관리용 kubeconfig다. 팀원 공용 파일로 배포하지 않는다. 개별 인증 방식과 RBAC를 정한 뒤 개인별로 제한된 kubeconfig를 제공한다. IAM Identity Center를 설정했다고 RKE2 신원이 자동으로 만들어지는 것은 아니다. 서버 join token은 사용자 로그인 자격으로 배포하지 않는다. [RKE2 CLI 도구](https://docs.rke2.io/reference/cli_tools), [클러스터 접근](https://docs.rke2.io/cluster_access), [토큰의 관리 권한](https://docs.rke2.io/security/token).

## 읽기 전용 사전 검사

`scripts/access_preflight.py`는 AWS CLI의 기존 인증만 사용한다. Python 표준 라이브러리로 동작하고 AWS 호출은 STS/SSO/EKS의 고정된 조회 목록에만 허용한다. CLI 자동 페이지 처리를 유지한다. 토큰, 계정 ID, ARN, 사용자 ID, 이메일, 클러스터 이름과 endpoint는 결과에 저장하지 않는다. 서비스 오류는 오류 코드만 남긴다.

```sh
# expected account는 검토한 로컬 배포 입력에서 설정한다.
# 현재 STS 계정을 그대로 복사하면 잘못된 계정 탐지 기능을 무력화하므로 그렇게 하지 않는다.
export BRAVO_EXPECTED_AWS_ACCOUNT='<검토한 대상 계정>'
python3 scripts/access_preflight.py \
  --region ap-northeast-2 --identity-center-region '<확인한 SSO 리전>' \
  > evidence.local.json
```

`--profile`로 기존 개인 프로필을 고를 수 있다. EKS가 준비되면 `--source-cluster`와 `--principal-role`을 지정해 해당 클러스터의 상태·access entry 지원과 역할 등록 여부를 확인한다. `--principal-role`은 같은 대상 계정의 IAM 역할만 허용하며 세션 ARN은 거부한다. 이 입력 값들은 공개 자료에 붙이지 않는다.

| 결과 | 의미 |
|---|---|
| `observed` | 지정한 항목을 읽고 제한된 조건을 확인 |
| `missing` | 조회 성공, 지정 범위에서 필요한 값이 없음 |
| `unknown` | 조회 오류 또는 불완전한 응답. 권한 없음과 자원 없음은 다름 |
| `not_checked` / `not_verified` | 입력/후속 검증이 없어 확인하지 못함 |
| `mismatch` | 검토한 계정과 현재 인증이 다름. 이후 조회 중단 |
| `not_ready` | 선택한 클러스터가 ACTIVE 또는 access entry 지원 조건을 충족하지 않음 |

종료 코드 0은 요청한 사전 조회 항목을 관측했다는 뜻이며, 2는 미확인/미준비 항목이 있다는 뜻이다. 잘못된 입력은 1이다. **`team_access`는 항상 `NOT_VERIFIED`**이며 개별 로그인·실제 RBAC 거부·DB 제어 시험을 이 도구가 했다고 말하지 않는다. permission set 할당 수는 의도한 팀원 목록과 일치 여부를 확인한 것이 아니다. RKE2는 아직 이 AWS 조회 도구의 검사 대상이 아니다.

## 배포 전 결정과 인계

1. Identity Center 조직 인스턴스의 리전·관리 주체와 기존 디렉터리 재사용 여부를 정한다.
2. 개인별 접근 범위, 그룹, 세션 기간, 해제 담당자, T6/T7 접속 경로를 확정한다. 적용 범위와 사용자 지시를 기록한다. 비밀 전달은 별도 승인된 경로로 한다.
3. T6/T7 환경이 준비된 뒤 개인별 권한을 적용한다. 이 문서는 실행용 `create-*` 명령을 제공하거나 자동 실행하지 않는다.
4. 운영자 본인 접속 → 각 팀원 본인 접속 → 허용/거부 시험 → T12/T15 DB/PgBouncer 소비자 시험 순서로 인계한다. 실제 값은 비공개 승인된 전달 경로로 공유한다.
5. 실행 시각·커밋·대상 범위·PASS/FAIL/UNKNOWN과 공개 가능한 근거만 기록한다. 전체 조건을 충족하고 PR이 병합된 뒤에만 해당 이슈 체크를 검토한다.

로컬 시험: `python3 -m unittest discover -s scripts/tests -p 'test_access_preflight.py' -v`.
