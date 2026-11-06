# T14 출발지 실제 감사 조회자의 임시 신원 검증

2026-11-06 **12:33:39–12:33:55 KST**, 실제 출발지 EKS의 별도 시험 namespace에서
권한 변경 카나리를 생성·삭제하고 CloudWatch에서 **16.05초 안에 두 이벤트**를 확인했다.
실제 임시 AWS 세션으로 STS 신원 확인과 CloudWatch 검색을 연결한 새 실행이다.

- 실행 ID: `603c18ca-dd2b-497c-84c0-5d625dba198b`
- AUD-01.3: PASS 1개 / 나머지 19개: 미실행 UNKNOWN. 전체 양쪽 PASS가 아니다.
- 비공개 ControlRun SHA-256: `15e6e6d077edbcbe98c941958925a572125fa174c0564b30e0f48f0bfd7625de`
- 비공개 최종 증거 SHA-256: `e9d66484e10a2f9f4d5d366b88643bdd688bcc5fe9a7c60b9ac3bb3063339c78`

## 실행 범위

1. 현재 출발지 Deployment·Pod·이미지·SA·Secret 메타데이터·Pooler 등록을 읽었다.
2. 고유 namespace에 **규칙이 비어 있는 Role**, 토큰 자동 mount가 꺼진 SA와 RoleBinding을
   만들었다. 이 SA에 실제 업무 권한이나 자격증명을 발급하지 않았다.
3. RoleBinding 생성 확인 후 UID·resourceVersion 조건으로 삭제하고 실제 부재를 확인했다.
4. 같은 임시 자격 snapshot으로 조회 역할 ARN·불변 RoleId·STS 세션을 확인한 뒤,
   고정된 출발지 CloudWatch 그룹/전체 audit stream에서 새 표식을 검색했다.
5. 첫 조회 NOT_RUN, 두 번째 조회에서 create/delete 두 사건을 확인했다. 신원·만료와
   검색 증거 지문을 묶어 비공개 영속 파일에 보존했다.
6. 기존 앱 등록은 전후 같았고, 자체 객체 4개·이번 SSM 터널을 정리했다.

앱·업무 DB·기존 정책은 변경하지 않았다. 생성 응답이 유실되면 같은 실행 label과 이름으로
본인 객체를 재확인하도록 시험 호스트를 구성했다. 남의 UID로 바뀐 자원은 삭제하지 않는다.
원본 자격·토큰은 메모리에서만 사용했으며 공개 결과에 포함하지 않았다.

## 코드 검증과 한계

- 신원/조회 결속 관련50개, 전체1,317개+58subtests 통과(기존 조건부5개 제외).
- 다른 앱·환경·시험·역할ID, 자격 만료, TLS/endpoint 설정 오염, 빈 검색, 권한 거부,
  저장 실패·시계 역행·실행 예산을 회귀 검증했다.
- 시험 당시 작업 트리의 파일 지문을 비공개 `tested-source-files.private.json`에 보존했다.
  코드 구현 커밋과 실제 시험 수행 시각을 별개로 기록한다.

조회자는 현재 본인 SSO 관리자 세션이다. **최소 권한 전용 조회 역할과 별도 거부 주체의
운영 인수는 아직 아니다.** 앱 namespace의 통제 시험·DB 감사의 중앙 전달·목적지·
T10 실제 계획 결속도 남아 있다. 이 부분 실행을 T14/T34 전체 완료로 체크하지 않는다.

구현: [조회 신원 wrapper](../../../services/control-runner/src/bravo_controls/audit_qualification.py),
[기존 임시 AWS 신원 검증](../../../services/control-runner/src/bravo_controls/aws_identity.py),
[호스트 책임](../../../services/control-runner/docs/audit-reader-identity.md).
