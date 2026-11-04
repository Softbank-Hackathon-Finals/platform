# AWS 통제 시험의 실제 신원 결속 (T14)

`QualifiedAwsProbe`는 기존 `SecretAccessProbe`·`S3AnonymousReadProbe`를 그대로 사용하며,
호출 전 실제 STS 신원을 확인한다. 역할 ARN을 입력했다는 사실만으로 앱 신원을 인정하지 않는다.
운영자가 로그인한 관리자 자격으로 시험한 결과를 앱 역할의 시험 결과로 바꿔 표시하지 않는다.

## 신뢰 경계

호스트가 승인된 환경 등록 정보에서 다음을 제공해야 한다.

| 입력 | 공급 책임 |
|---|---|
| 앱·환경·Secret/객체와 고정 버전 | 현재 서버 등록 정본. 요청 본문에서 임의 선택하지 않음 |
| 역할 ARN과 고유 IAM `RoleId` | 역할 등록 시 신뢰된 AWS readback. 이름이 같은 재생성 역할은 재등록 필요 |
| `TemporaryCredentials` | 기존 Credential Provider가 발급한 단기 역할 세션과 실제 만료 시각 |
| `evidence_sink(bytes)` | 서버의 접근 통제·내구성·보존 정책을 갖춘 증거 저장소 |
| 평가 프로필·Runner | 기존 T5 정본. 연결되지 않은 시험도 UNKNOWN으로 남김 |

이 패키지는 역할을 생성하거나 인수하지 않는다. 사람 로그인·MFA, Credential Provider,
발급 권한 및 세션 정책을 대신하지 않는다. 브라우저·AI·업로드한 앱이 자격 증명이나 기대
RoleId를 제공하는 API로 노출해서는 안 된다.

## 실행

1. 앱·환경·시험 방법이 등록값과 같을 때만 Credential Provider를 호출한다.
2. 실제 만료까지 최소 60초 남은 임시 자격 증명 스냅샷을 받는다. 장기 IAM 키는 거부한다.
3. 같은 명시적 스냅샷으로 STS와 서비스 SDK 클라이언트를 만든다. SDK의 기본 자격 증명
   체인으로 대체하지 않는다. STS의 계정·assumed-role ARN·UserId의 고유 RoleId와 세션명을 대조한다.
4. 앱 역할의 허용 대조군과 다른 역할/익명 접근을 기존 probe로 수행한다. SDK 요청의 실제
   성공·거부를 판정하며, 만료된 토큰이나 연결 오류를 정상 접근 거부로 세지 않는다.
5. 신원 확인 시각·만료·신원 해시와 해당 run의 probe 증거를 하나로 보존한다. 반환된
   `output_sha256`은 이 전체 envelope 바이트의 해시다. 보존 실패 시 결과를 반환하지 않는다.
6. 클라이언트는 성공·실패 모두 닫는다. 시간 역행·시험 중 만료는 거부한다.

```python
from bravo_controls.aws_identity import RegisteredRole
from bravo_controls.aws_qualification import QualifiedAwsProbe

# 모두 인증된 호스트의 등록 정보/함수. 사용자 업로드 JSON을 사용하지 않는다.
probe = QualifiedAwsProbe(
    registered_secret_target,
    app_role=RegisteredRole(registered_app_role_arn, registered_app_role_id),
    other_role=RegisteredRole(registered_other_role_arn, registered_other_role_id),
    credential_provider=issue_registered_temporary_session,
    evidence_sink=retain_private_evidence,
)
# 기존 Runner의 probes에 SEC-01.1로 등록한다. S3 ObjectTarget은 other_role 없이
# IAM-01.1로 등록하며, 음성 대조는 자격 증명 없는 일반 GET이다.
```

AWS의 [GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html)는
호출 신원을 확인하는 API이며 서비스 접근 권한이나 권한 회수 여부를 증명하는 API가 아니다.
따라서 실제 허용·거부 요청과 함께 사용한다. 환경·프로필의 endpoint 덮어쓰기를 무시하고
명시적 TLS 인증서 검증, 재시도 1회, 연결 3초·읽기 5초 제한을 적용한다.
설정 의미는 [Botocore Config](https://docs.aws.amazon.com/botocore/latest/reference/config.html)를 따른다.
첫 경로는 AWS regional endpoint이며 사용자 지정 endpoint·프록시·VPCE 별도 경로는 지원하지 않는다.

## 보안 및 한계

- 자격 증명·Secret 본문·SDK 예외 원문·호스트 주소는 증거에 넣지 않는다. 자격 증명 객체의
  `repr`에서도 키·토큰을 제외한다. 인증 정보는 메모리에 존재하므로 별도 신뢰 워커에서만 실행한다.
- Botocore/urllib3 DEBUG 로깅이 활성화되어 있으면 시작하지 않는다. 호스트의 tracing,
  crash dump, APM, 임의 코드·로깅 설정 변경까지 이 라이브러리가 통제하지는 않는다.
- STS와 서비스는 한 번 고정한 자격 증명만 사용한다. 서비스 자격이 만료/회수되면 UNKNOWN으로
  처리될 수 있다. 과거 증거의 재사용 가능성은 T19의 만료·현재 계획 결속에서 다시 검사한다.
- SDK의 요청별 timeout은 전체 작업의 확정 종료 시한이 아니다. 호스트가 워커 격리·전체
  deadline·취소·증거 저장 시간 제한을 적용해야 한다.
- 고유 RoleId는 등록 정본의 신뢰성에 의존한다. 신원 확인과 실제 정책 시험을 결합해도
  정책·객체의 원자적 스냅샷이나 시험하지 않은 모든 AWS 권한의 최소권한을 증명하지 않는다.

## 검증 상태와 실환경 인수

실제 Botocore 클라이언트와 Stubber를 사용해 같은 자격 증명 사용, endpoint/CA 환경 덮어쓰기
무시, IAM 사용자·다른 계정·다른 역할·같은 이름의 재생성 역할 거부, 만료·시간 역행·증거 보존
실패·민감값 미출력을 회귀 시험했다. **AWS 실계정 요청을 수행한 결과가 아니다.**

팀 환경의 소비자 역할과 접속 경로가 인계되면 다음을 실제로 확인해야 한다.

1. 등록된 앱/다른 역할 각각의 STS 신원 및 실제 IAM RoleId 대조.
2. 시험 전용 Secret의 앱 읽기 허용·다른 역할 거부와 현재 버전 일치.
3. 시험 전용 버전 관리 객체의 앱 전후 읽기·익명 일반 GET 거부.
4. 만료/회수 자격의 UNKNOWN, 저장 실패 시 PASS 미발행, T19에 같은 증거 참조 전달.

현재 팀 계정 인증·kubeconfig 인계가 완료되지 않은 상태이므로 이 PR 병합만으로
T14 전체 완료, 양쪽 환경 통제 동등성 또는 기업·금융 규정 준수를 표시하지 않는다.
