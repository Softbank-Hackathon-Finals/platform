# T14: 같은 실제 계획의 출발지·목적지 앱 → PgBouncer TLS 시험

2026-11-06 KST에 실제 AWS EKS 출발지와 온프렘 RKE2 목적지에서 각각
새 실행을 수집했다. 양쪽 원문은 같은 T10 등록 계획의 `workload_id`, 환경 쌍,
`full:1` 프로필에 맞춰 생성했다. 원문 ID·시각을 다시 쓰거나 다른 실행의
PASS를 합치지 않았다.

## 실제 시험 결과

| 시험 | 출발지 | 목적지 | 확인한 동작 |
|---|---|---|---|
| ENC-01.1 | PASS | PASS | 인증서를 검증한 TLS 연결과 읽기 전용 `SELECT 1` |
| ENC-01.2 | PASS | PASS | 잘못된 CA·호스트명 각각 거부, 전후 정상 연결 성공 |
| ENC-01.3 | PASS | PASS | 비밀번호 없는 평문 시작 거부, 전후 정상 연결 성공 |

각 원문은 full 프로필의 20개 행을 포함한다. **각각 3개 PASS,
17개 UNKNOWN/NOT_RUN**이며 전체 통제 PASS가 아니다. 과거의 Secret 암호화
시험 결과도 이번 TLS 실행에 합치지 않았다.

수집기 입력과 결과는 [summary.json](summary.json)의 원문·증거 해시로
식별한다. 실제 식별자와 원문은 접근 제한된 비공개 경로에 보존했다.

## 수집 경계

- 출발지 앱의 이미지·ReplicaSet·서비스 계정을 현재 계획의 등록 영수증과
  대조했다. 목적지 클러스터와 CNPG도 등록 환경의 UID 지문과 일치했다.
- 양쪽에서 앱·Pooler·Service·EndpointSlice·CA·자격 참조를 실행 전후와
  각 시험의 전후에 확인했다. 각 실행은 상태 대조 8회와 증거 3개를 보존했다.
- 비밀번호는 기존 앱 프로세스 안에서만 사용했다. 정상 연결은 고정된 읽기
  전용 쿼리만 실행했고, 거부 시험에는 비밀번호를 보내지 않았다.
- 목적지는 기존 앱의 loopback Secrets Manager 호환 서비스를 사용했다.
  이는 현재 앱의 자격 조회 경로이며 OpenBao로의 최종 자격 이전이나
  외부 비밀 저장소의 운영 보안 검증을 뜻하지 않는다.
- 목적지 ARM64용 native 의존성 한 개는 기존 lockfile에 고정된 공식 wheel의
  크기·SHA-256을 검증했다. 앱 이미지·배포·업무 데이터·공유 정책은 바꾸지 않았다.
- 이번 세 시험은 **앱 → Pooler 구간**만 확인한다. Pooler → DB, 논리 복제,
  DB 전환·데이터 무결성·무중단 자격은 별도다.

## T19와 콘솔 점검

기존 T19 결정적 판정기로 양쪽 새 원문을 로컬에서 비교했다. 세 행은
`BOTH_PASS`, 나머지 17행은 `SOURCE_UNKNOWN`이며 전체는 BLOCK이다.
이 계산은 **운영 호스트의 인증·현재 권한·불변 컨텍스트 저장을 거치지 않은
진단 결과**다. 운영 게이트 보고서나 전환 권한으로 등록하지 않았다.

실제 계획 필드를 사용한 콘솔 projection은 20개 행을 수용했고, 다른 앱·프로필
버전·목적지 입력 세 경우를 거부했다. 기존 브라우저 시험은 이 진단 보고서를
로컬의 fixture 인증·fixture 계획으로 표시했다. Chromium·WebKit 두 시험에서
BLOCK, 20개 행, 승인·실행 버튼 비활성화, 쓰기 요청 없음, 로그아웃 후 결과
제거를 확인했다. **이는 운영 로그인·T10 저장·SSE 인수의 완료가 아니다.**

## 운영 인계와 남은 연결

양쪽 원문·증거 6개·실제 계획 결속·manifest, 총 10개 파일을 팀 플랫폼의
별도 immutable Secret에 보관하고 UID와 바이트를 재조회했다. 기존 암호화
시험 인계 원문은 그대로 보존했다. 진단 보고서는 이 운영 인계에 등록하지 않았다.

T10 담당은 기존 신뢰 호스트의 `GateRecords.record_run`·`read_run` 연결을
맡기로 확인했다. [인계와 담당 답변](https://github.com/Softbank-Hackathon-Finals/platform/issues/29#issuecomment-6678133656).
현재 주체 권한을 확인한 같은 계획의 저장·재조회, T19 운영 인증·판정 저장,
운영 콘솔 조회는 남아 있다. T14·T19 완료 체크를 추가하지 않는다.

## 출처와 재현 한계

- [collector-source-manifest.json](collector-source-manifest.json): 실제 수집에 사용한 저장소 소스.
- [consumer-source-manifest.json](consumer-source-manifest.json): 비교·콘솔 점검 시의 저장소 소스.
  그 사이 추가된 재대조 프로필은 기존 `full:1` 시험 정의를 변경하지 않았다.
- [private-transport-manifest.json](private-transport-manifest.json): 비공개 실행·전달 스크립트의 지문.

공개 요약은 원문이 아니며 해시는 서명·실행 권한이 아니다. 공개본만으로
비공개 원문이나 운영 자원의 실체를 독립 확인할 수는 없다. 증거 만료는
판정 유효기간이며 인계 Secret의 자동 삭제를 뜻하지 않는다.
