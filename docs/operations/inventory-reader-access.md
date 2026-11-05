# T16 수집용 reader 인수

사람의 기존 SSO 로그인과 수집 프로세스의 읽기 권한을 분리한다.
`scripts/inventory_reader.py`는 이름이 확인된 사람의 SSO로 전용 reader를
15분간 AssumeRole한다. 출발지 API에는 그 reader의 EKS 토큰만 사용한다.
SSO 관리자 자격을 inventory 프로세스에 넘기지 않는다.

이 도구는 IAM, EKS 접근 entry, RBAC, 등록, 계획을 만들거나 수정하지 않는다.
T7 담당이 확인한 비공개 설정과 이미 준비된 접근 경로를 소비한다.
공식 T16 생산자가 `scan`을 실행하며, 접근 담당은 `verify`까지만 실행한다.

## 접근 범위

출발지 namespace의 Deployment, ServiceAccount, CNPG Cluster `get/list`만
Kubernetes Role로 부여한다. 전용 IAM 역할의 STANDARD EKS access entry에
이 RoleBinding의 그룹을 연결한다. EKS 관리형 접근 정책은 연결하지 않는다.
기존 공유 collector, 사람의 계정 할당, 앱의 IRSA 역할은 보존한다.
[AWS의 access entry와 RBAC 안내](https://docs.aws.amazon.com/eks/latest/userguide/access-policies.html)

병합된 [#371](https://github.com/Softbank-Hackathon-Finals/platform/pull/371)의
실제 호출에 맞춰 다음 metadata 읽기만 정확한 자원 ARN에 허용한다.

| 자원 | 허용 동작 |
|---|---|
| 해당 namespace의 IRSA 역할 | `iam:ListAttachedRolePolicies`, `iam:ListRolePolicies`, `iam:GetRolePolicy` |
| 역할 정책에서 참조한 버킷 | `s3:GetBucketPolicyStatus`, `s3:GetBucketLocation` |
| 참조한 Secret | `secretsmanager:DescribeSecret` |
| 참조한 KMS 키 | `kms:DescribeKey` |

현재 두 역할에는 관리형 정책이 없다. 관리형 정책이 추가되면 일단 중단하고
해당 정책 ARN에 대한 `iam:GetPolicy`와 `iam:GetPolicyVersion` 범위를 검토한다.
Secret 값 읽기, KMS 복호화, 객체 본문 읽기·쓰기, 다른 역할 조회는 허용하지 않는다.
STS 신원 조회는 별도 접근 영수증이며 T16의 `inputs.api_calls`로 꾸미지 않는다.

신뢰 정책은 기존 SSO 역할과 허용한 `aws:userid`에 결속한다. 공유 키나
CI·노드 자격을 추가하지 않는다. 실제 실행의 upstream SSO 신원과 reader
신원은 각각 비공개 영수증에 남긴다. 운영자의 시험을 생산자 본인 인수로
계산하지 않는다.

## 실행 준비

비공개 인수 설정에는 계정·리전, 정확한 SSO UserId, reader ARN/RoleId,
원본 EKS endpoint·CA, 승인된 SSM 경유 호스트, namespace·리소스 UID,
현재 이미지·source SHA, 기존 등록 이미지·source SHA, inventory revision,
실제 역할·버킷·Secret·KMS ARN과 접근 종료 시각이 들어간다.
설정에는 자격값이 없다. 디렉터리는 `0700`, 설정은 `0600`으로 전달한다.

생산자의 Mac에서 Python 3.12+, AWS CLI v2, Session Manager plugin, `uv`가
필요하다. 기존 본인 SSO로 로그인한다. 정확한 포털·계정·역할은 보호된
인수 안내를 사용하며 다른 사람의 프로필이나 SSO 캐시를 복사하지 않는다.

기존 SSM 포트 포워딩 절차로 승인된 호스트에서 해당 EKS endpoint의 443까지
본인 루프백 터널을 연다. 사용 중인 포트를 재사용하지 않는다.
helper는 원본 hostname·CA를 검증한다. TLS 검증을 끄거나 공개 endpoint를
추가하지 않는다. 종료할 때 이번에 연 SSM SessionId만 종료한다.

```sh
python3.12 scripts/inventory_reader.py verify \
  --config /private/path/reader.json --profile bravo-personal \
  --port 18516 --output-dir /private/path/new-access-run
```

`verify`는 reader로 세 원본 export와 AWS 메타데이터를 읽고, Kubernetes
SelfSubjectAccessReview의 쓰기·Secret·범위 밖 거부와 무자격 HTTP 401,
범위 밖 IAM 실제 AccessDenied를 확인한다. SSAR은 실제 파괴적 요청이 아니다.
export는 권한 제한 경로에 원본으로 저장한다. 자격값으로 보이는 literal env는
저장 전에 거부한다. Secret 이름 등은 인수 설정에 명시한 정확한 참조만 허용한다.

`verify`의 PASS는 접근 검증 결과다. `registration_matches: false`면 등록 담당이
현재 source SHA와 이미지로 유효한 입력을 인계해야 한다. 이전 등록을 도구가
자동으로 바꾸거나 mismatch를 허용하지 않는다. 기존 수집 역할의 제한된
증거를 보존하고 새 원문이 필요한 동작만 확인한다.

## 생산자의 한 번 수집

등록이 현재 이미지·source SHA와 일치하고 source/플랫폼 checkout이 지정한
깨끗한 커밋일 때만 실행한다. 플랫폼 checkout은 #371을 포함한 인수 revision이다.

```sh
python3.12 scripts/inventory_reader.py scan \
  --config /private/path/reader.json --profile bravo-personal \
  --port 18516 --repo /private/path/demo-chat \
  --platform /private/path/platform --output-dir /private/path/new-collection-run
```

이 명령은 source 수집용이다. 목적지 profile/export 인수와 T32 후보 관측은
기존 T16 생산자가 별도로 조율한다. 임의의 target 관측을 만들어 넣지 않는다.
`aws.json`은 생성하지 않는다. 새 15분 reader 자격만 환경으로 주입하고,
기존 AWS profile·credential file·IMDS·컨테이너 자격과 endpoint override를
제외한 프로세스에서 `uv run --frozen --extra aws bravo-inventory scan`을 부른다.
토큰·키는 파일·argv·로그로 복사하지 않는다.

기존 output 디렉터리는 인증 전에 거부한다. scan 시작 표식을 먼저 남기고
실패·중단은 `UNKNOWN`으로 보존한다. 다른 이름을 붙여 무작정 재실행하지 말고
원본 output, 종료 상태, `scan-started.json`을 먼저 대조한다. 성공하면 원본
inventory, export 해시, 신원·시각·revision, `inputs.api_calls`를 기존 소비 담당에게
보호된 경로로 전달한다. 이 목록은 중복 제거된 호출 종류이며 CloudTrail 전체
호출 횟수나 독립 실행 증명은 아니다.

## 만료와 회수

helper는 설정의 종료 시각 이후 시작하지 않는다. STS 요청 수명은 900초이며
실제 반환된 만료를 영수증에 기록한다. 역할 최대 세션은 AWS 최소 상한인
1시간이다. 신뢰 정책의 종료 시각 이후 AssumeRole을 막고, 권한 정책에도
종료 시각 이후 Deny를 둔다. EKS 접근 entry 자체의 자동 삭제를 뜻하지 않는다.

종료 때 T7 담당은 보존한 ARN·RoleId와 Kubernetes UID를 대조해 이번 전용
RoleBinding/Role, access entry, inline policy, IAM role만 회수한다.
기존 사람 역할·collector·등록·소스 업무 자원과 다른 SSM 세션은 건드리지 않는다.
이미 발급한 자격의 만료와 실제 접근 부재는 별도 읽기로 확인한다.
