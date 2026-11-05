# AWS Secret 저장 암호화 확인 (ENC-02.1)

`SecretEncryptionProbe`는 등록된 Secret의 현재 버전과 **기존** KMS 키의 메타데이터를
전후 재조회한다. 비밀 본문을 읽거나 복호화·교체·재암호화하지 않는다.
운영자가 제공한 고유 IAM RoleId와 단기 자격증명으로 STS 신원을 확인하고,
같은 자격 스냅샷에서 Secrets Manager·KMS의 SDK 클라이언트를 만든다.
메타데이터 조회 권한은 앱 자체의 업무 권한과 다를 수 있으며, 결과에서 둘을 혼동하지 않는다.

## 판정 조건

- 실제 Secret ARN·AWSCURRENT 버전과 등록값이 일치한다.
- 연결 키가 등록된 동일 계정·리전의 KMS key ARN/ID와 일치한다.
- `ListSecretVersionIds`를 끝까지 읽고, 정확한 현재 버전의 `KmsKeyIds`가
  등록한 키 하나와 일치하는지 전후 확인한다. 설정의 `KmsKeyId`만으로 통과시키지 않는다.
  키 변경 시 기존 버전의 재암호화가 실패할 수 있으므로, 현재 버전의 키 목록 누락·다른 키·
  여러 키·중복 버전·미완료 페이지·조회 거부는 UNKNOWN이다. 현재 버전이 뒤 페이지에 있어도
  확인하며, 앞 페이지에서 찾았더라도 나머지 페이지를 생략하지 않는다.
- 키는 `Enabled`, 대칭키, 암호화/복호화 용도다.
- 기본 AWS 관리 키를 사용하는 경우, 이미 존재하는 `alias/aws/secretsmanager`와
  실제 TargetKeyId의 일치를 `ListAliases(KeyId=고정 키 ARN)`으로 확인한다.
- 전후 관찰·현재 버전·신원이 유지되고 시간 역행·만료·예산 초과가 없다.
- 증거 저장이 성공한 뒤에만 관찰을 반환한다.

이 경우에만 `enabled`를 반환한다. 키 미확인·권한 거부·삭제 예약·버전 변경·미완료 목록은
UNKNOWN이다. 키가 비활성화됐다는 사실을 저장 데이터가 평문이라는 뜻으로 바꾸지 않는다.
사용자 정의 alias 참조, 교차 계정·리전, 외부 키 저장소의 동등성은 별도 자격 대상이다.

## 읽기 전용 경계

`DescribeSecret`·`ListSecretVersionIds`·고정 key ARN의 `DescribeKey`·`ListAliases`만 호출한다.
Secret 값, KMS 키 재료, 정책·grant·회전·생성·삭제 API는 사용하지 않는다.
**키가 없는 사전 정의 alias에 DescribeKey를 호출하면 AWS 관리 키가 생성될 수 있으므로**
이 경로에서는 alias로 DescribeKey를 호출하지 않는다. 등록 작업도 기존 alias/키를 읽어야 한다.

SDK는 검증하는 HTTPS, 고정 regional endpoint, 명시적 임시 자격, 재시도 1회와
I/O 기한을 사용한다. 목록은 최대 5페이지다. 호출 사이의 60초 예산 확인은 외부 요청의
즉시 취소를 보장하지 않으므로, 호스트는 전체 워커 기한과 종료를 별도로 관리한다.
로그 DEBUG 금지와 호스트 자격증명 보호는 기존 `verified_client` 경계를 따른다.

공개 증거에는 자원·현재 버전의 결속 해시, 신원 해시, 시각, 고정 관찰 사실만 남긴다.
키 정책의 최소권한·회전·침해 대응, 모든 데이터의 저장 암호화 또는 규정 준수를
이 한 시험으로 증명하지 않는다. 운영 등록·생산자 인증·최종 게이트는 T10/T19가 담당한다.

## 근거

- [AWS DescribeSecret](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_DescribeSecret.html):
  비밀 값을 제외한 메타데이터와 KmsKeyId. 기본 AWS 관리 키 사용 시 해당 필드는 생략된다.
- [AWS DescribeKey](https://docs.aws.amazon.com/kms/latest/APIReference/API_DescribeKey.html):
  키 상태·용도·관리 주체, 사전 정의 alias의 키 생성 가능성과 메타데이터 조회의 한계.
- [AWS SecretVersionsListEntry](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_SecretVersionsListEntry.html):
  버전별 실제 암호화 키 `KmsKeyIds`.
- [AWS UpdateSecret](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_UpdateSecret.html):
  새 키의 암호화 권한이 없으면 기존 버전을 새 키로 재암호화하지 않는다.

검사 도구 버전 2부터 위 버전별 키 결속을 확인한다. 버전 1의 설정 키 확인 결과는
현재 버전의 실제 암호화 키를 증명하지 않으므로 게이트 근거로 재사용하지 않는다.

단위 시험은 실제 Botocore의 Stubber이며 실계정 성공을 뜻하지 않는다. 실제 환경 실행은
등록된 자원·신원과 증거 저장 경로를 별도로 확인해야 한다.
