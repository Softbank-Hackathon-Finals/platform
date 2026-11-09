# T1 사람용 개발자 IAM 준비

11/3 후속 지시에서 개발 병목을 줄이기 위해 개인 IAM 주체를 먼저 준비하고, 사람 개발자에게 넉넉한 권한을 부여하도록 요청했다. 이 도구는 **사람용 AdministratorAccess**와 제품 런타임의 제한된 권한을 별도로 관리한다. 기존 관리자를 재사용하고 검증된 사람 매핑만 추가한다. 사용자 초대 메시지나 자격 전달은 수행하지 않는다.

## 범위

- 사람 개발자: 검토된 로스터에 있는 개인 IAM 사용자. 새 사용자는 `/team-bravo/humans/` 경로와 `project`, `component`, `principal-type=human`, `github-login` 태그로 관리한다.
- 권한: 전용 `bravo-human-developers` 그룹에 AWS 관리형 `AdministratorAccess`를 연결한다. 기존 개인 주체가 이미 이 정책을 보유하면 기존 연결을 유지한다.
- 변경 제외: AI 워커, 리허설 서비스 계정, 수집기, Executor, Actions OIDC 등 제품/CI 역할. 이 도구는 `attach-role-policy`를 허용하지 않는다.
- 인증: 새 사용자에 콘솔 프로필이 없으면 최초 암호 변경을 요구하는 임시 프로필을 준비할 수 있다. 기존 프로필은 보존하고 암호를 바꾸지 않는다. **장기 access key는 생성하지 않는다.**

AdministratorAccess는 AWS 서비스와 자원에 대한 전체 허용 정책이다. 이 정책이 붙었다는 사실과 조직 SCP·권한 경계·서비스별 인증까지 통과했다는 사실은 구분한다. [AWS 정책 원문](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AdministratorAccess.html).

## 실행 전 입력

로스터는 계정 ID·실제 IAM 이름을 포함하므로 저장소 밖의 사용자 전용 파일로 둔다. `expected_account`는 검토한 배포 입력에서 가져오며 현재 STS 결과를 무조건 복사하지 않는다. 각 항목의 `github_login`과 기존 사용자 연결을 실제 팀 명단 및 IAM 조회로 대조한다.

```json
{
  "expected_account": "111122223333",
  "members": [
    {
      "alias": "member-01",
      "github_login": "example-owner",
      "user_name": "existing-personal-identity",
      "principal_type": "human",
      "reuse_existing": true
    },
    {
      "alias": "member-02",
      "github_login": "example-developer",
      "user_name": "bravo-example-developer",
      "principal_type": "human"
    }
  ]
}
```

위 값은 예시다. 사용자 파일을 mode 0600으로 두고 커밋하지 않는다. 공개 결과에는 `member-01` 같은 별칭과 상태만 들어간다. 중복 GitHub 태그, 같은 사람 이름의 기존 IAM 사용자, 다른 용도의 이름 충돌, 전용 그룹에 로스터 밖 주체가 있는 경우 실행을 멈추고 매핑을 다시 검토한다. 다른 사람/런타임 그룹을 관리자 그룹으로 바꾸지 않는다.

## 계획 · 적용 · 재확인

```sh
python3 scripts/provision_human_access.py plan --config /private/path/human-access.json --with-console
swiftc scripts/iam_console_keychain.swift -o /private/path/iam-console-keychain
/private/path/iam-console-keychain --probe
uv sync --project scripts/iam-console --locked --no-dev
python3 scripts/provision_human_access.py apply \
  --config /private/path/human-access.json --with-console \
  --keychain-helper /private/path/iam-console-keychain \
  --sdk-python scripts/iam-console/.venv/bin/python
python3 scripts/provision_human_access.py verify --config /private/path/human-access.json
python3 scripts/provision_human_access.py plan --config /private/path/human-access.json --with-console
```

마지막 계획의 `actions: []`가 재실행 시 추가 변경이 없다는 확인이다. `apply`는 작업 직전에 계정·현재 사용자를 다시 읽는다. 원격 쓰기의 응답을 잃거나 오류가 나면 자동으로 같은 mutation을 재시도하지 않는다. `verify`/`plan`으로 현재 상태를 다시 읽고 남은 변경을 판단한다. 일부 적용을 이전 단계까지 자동으로 되돌리거나 기존 주체를 삭제하지 않는다.

AWS 호출 클라이언트는 사용자·그룹 생성, 정책 연결, 그룹 가입만 쓰기 목록에 둔다. 키 생성·기존 암호 변경·역할 정책 변경은 목록에 없다. IAM 호출 원문 응답과 오류 메시지에는 식별값이 들어갈 수 있어 표준출력에는 반환하지 않고 오류 코드만 남긴다.

## 임시 콘솔 암호

네이티브 macOS 도우미는 먼저 임의의 시험 항목을 Keychain에 저장·비노출 재조회·삭제한다. 실제 임시 암호는 `SecRandomCopyBytes`로 메모리에서 생성하고, 로컬 로그인 Keychain의 `Team Bravo initial console password` 항목에 저장한 뒤 같은 값을 다시 읽어 대조한다. Keychain 서비스명은 대상 계정의 해시로 분리하며 실제 계정 ID를 출력하지 않는다.

네이티브 도우미는 격리된 Python SDK 도우미에 메모리 파이프로 JSON을 전달한다. SDK가 IAM `CreateLoginProfile`을 호출하고 재조회한다. AWS CLI의 인자·입력 파일·CLI history에는 암호가 들어가지 않는다. 비밀번호를 소스, 평문 임시 파일, 로그, 명령행 인자에 남기지 않는다. SDK 로깅과 core dump는 꺼져 있으며 오류 원문도 출력하지 않는다. 표준출력은 생성 여부·Keychain 재조회 결과·암호 변경 필요 여부만 담는다. Keychain 또는 SDK 준비 검사가 실패하면 콘솔 프로필을 포함한 적용이 원격 변경 전에 멈춘다. [AWS SDK 작업 정의](https://docs.aws.amazon.com/boto3/latest/reference/services/iam/client/create_login_profile.html).

기존 콘솔 프로필이 있으면 그대로 보존한다. 기존 Keychain 항목은 이전 시도의 보관된 암호로 재사용하며 임의 삭제하지 않는다. Keychain 저장 뒤 AWS 응답이 유실되더라도 다시 조회해 프로필이 이미 있으면 생성/재설정하지 않는다. 도우미 자체도 계정 일치와 대상 사용자의 사람용 경로·태그를 확인한다.

이 도구는 암호 출력 명령을 제공하지 않는다. 운영자가 필요한 항목을 찾는 절차와 실제 계정 매핑은 저장소 밖의 로컬 인계에만 둔다. 비밀 전달은 별도로 정한 승인된 경로에서 수행하고, 수신자가 처음 로그인해 암호를 변경하고 개인 인증 수단을 등록한 뒤 실제 로그인 성공을 확인한다. 아직 전달하지 않은 암호를 전달 완료로 기록하지 않는다.

최종 도우미는 Keychain UI 상호작용을 끈 상태로 실행한다. 기존 항목 접근에 운영자 확인이 필요하면 기다리거나 ACL을 바꾸지 않고 실패한다. 사용자는 Keychain 앱에서 해당 항목과 신뢰하는 도우미를 직접 확인한 뒤 재개할 수 있다. `plan`으로 현재 상태를 먼저 읽으며, 이미 생성된 콘솔 프로필이나 보관된 암호를 재생성하지 않는다. 초기 Keychain 시험 성공이 모든 기존 항목의 접근 권한을 보장하지는 않는다.

## 완료 판단

로컬 검증은 `python3 -m unittest discover -s scripts/tests -v`와 `python3 scripts/ci_project.py scripts/iam-console python`이다. SDK 도우미·시험·잠금 의존성은 `scripts/iam-console` 프로젝트가 소유한다. 공통 CI 선택기가 이 프로젝트를 발견하므로 SDK 코드·시험·의존성 변경은 필수 `platform` 결과에 포함된다. 기본 보드 도구는 SDK를 import하지 않는다. Keychain 검증은 macOS에서만 수행한다.

IAM 사용자·그룹·정책·콘솔 프로필을 읽어 확인한 결과는 **접근 준비**다. 실제 팀원 로그인, MFA 등록, SSO 전환, EKS access entry/RBAC, RKE2 개별 신원, DB/PgBouncer 제어 시험은 별도다. 새 access key 수 0, 런타임 역할 변경 없음, 외부 메시지 전송 없음도 실행 영수증에 구별해 적는다.

기존 [사전 조회](access-readiness.md)와 [처음의 읽기 전용 증거](evidence/t1-access-readiness-2026-11-03/README.md)는 IAM 적용 전 시점 기록이다. 현재 적용은 [IAM 준비 증거](evidence/t1-human-access-2026-11-03/README.md)를 본다. 사람용 개발 AdministratorAccess를 영구 운영 권한으로 그대로 확정한 것은 아니다. 개발 종료 시 사용자/그룹·권한·인증 수단을 다시 검토한다.
