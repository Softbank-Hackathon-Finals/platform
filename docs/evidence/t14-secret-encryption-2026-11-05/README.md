# T14 실제 AWS Secret 저장 암호화 확인

**자격 철회:** 아래 실행은 도구 버전 1의 진단 기록으로 보존한다. PR #306 리뷰에서
설정 키와 현재 버전의 실제 암호화 키가 다를 수 있음이 확인됐다. 따라서 이 기록의
PASS는 ENC-02.1 게이트 근거로 사용할 수 없다. 원문은 덮어쓰지 않으며, 버전 2의
`ListSecretVersionIds.KmsKeyIds` 전후 확인을 포함한 별도 실행으로 재검증했다.

## 수정 후 별도 실행

11월 5일 **21:28 KST**, 커밋 `98990a40399fe42d99add09a9fc65bf869e9c042`의 도구 버전 2로
새 실행 `7bf946f7-87e7-4d24-898b-9d3b6d011fc6`을 수행했다. ENC-02.1은 PASS이며
나머지 19개 시험은 UNKNOWN/NOT_RUN이다.

- [새 원문](version-2/control-run.json), [새 증거](version-2/evidence.json),
  [새 요약](version-2/summary.json), [실행 소스](version-2/source-manifest.json)
- 현재 버전의 실제 `KmsKeyIds`를 완전한 버전 목록에서 전후 대조했다.
- 앱 등록 전후 2회 일치, 비밀 값 조회·KMS 변경 0건. 종료 후 이번 SSM 세션을 회수했다.
- 설정 키 변경 뒤 기존 버전 재암호화 실패를 재현한 두 사례는 수정 전 실패·수정 후 차단했다.
  관련 64개 및 전체 950개 시험 통과(환경 의존 5개 제외, 하위 시험 58개 통과).

새 실행도 운영자 메타데이터 신원에 한정된다. 앱 권한·운영 등록·목적지·최종 양쪽 게이트의
인수를 대신하지 않는다. 아래 원래 실행의 자격 철회는 유지한다.

## 원래 실행 (자격 철회)

11월 5일 **20:18:48–20:19:01 KST**, 등록된 출발지 Secret의 현재 버전과 기존 KMS 키를
`SecretEncryptionProbe` 버전 1로 전후 조회했다. 당시 출력은 PASS였으나 위 이유로 자격을 철회한다.

- 실행: `8c42b802-eb20-4946-881d-37dcce14af1f`
- [원문](control-run.json), [증거](evidence.json), [요약](summary.json), [실행 소스](source-manifest.json)
- 실제 실행 코드: `265d817` (증빙 커밋을 과거 실행 소스로 표시하지 않음)
- 앱 등록을 전후 두 번 대조하고 Secret ARN·AWSCURRENT 버전·같은 계정/리전의 KMS key ARN/ID 확인
- 기본 AWS 관리 키의 기존 alias와 TargetKeyId 일치, 활성 대칭 암호화 키·AWS KMS origin 확인
- 두 SDK는 같은 운영자 SSO 임시 자격 스냅샷으로 각각 STS 신원을 확인
- 비밀 본문·키 재료 조회 0건. 키 생성·회전·정책·grant·암호화 설정 변경 0건
- Secret과 키의 metadata는 같은 실행 안에서 재조회하고, 증거는 fsync 후 반환

운영자의 **메타데이터 조회 신원**을 사용했으며 앱의 Secret 접근 허용·거부나 최소권한을
증명한 시험이 아니다. ARN·고유 RoleId·버전·키 참조의 원본 등록과 운영자 스크립트는
비공개 보존하고, 공개 증거에는 결속·신원 해시와 고정 관찰 사실만 남겼다.

이 실행의 다른 19개 시험은 UNKNOWN/NOT_RUN이다. 앞선 TLS·객체·소스 검사와
결과를 합쳐 하나의 전체 PASS로 만들지 않았다. 키 정책·회전·복구 가능성이나 전체 데이터의
저장 암호화, 목적지 OpenBao·RKE2, T10 운영 등록·최종 양쪽 게이트는 별도 인수다.

검사 의미와 공식 API 근거는 [검사기 안내](../../../services/control-runner/docs/secret-encryption.md)를 따른다.
이 메타데이터 확인만으로 금융 규정 준수·T14 전체 완료·컷오버 승인을 표시하지 않는다.
