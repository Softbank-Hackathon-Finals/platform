# T12 실제 제어 경로와 출발지 풀러의 목적지 이름 해석

플랫폼 EC2/RKE2에서 출발지 EKS·목적지 개발 VM으로 가는 DB/PgBouncer 제어 연결과 실행 기록 DB를 검증했다.
이 기록은 [#134](https://github.com/Softbank-Hackathon-Finals/platform/pull/134) 병합 뒤의 연결 준비 결과다.
실제 컷오버·승인된 목적지 앱·T19/SHIFT·운영자 로그인 완료를 뜻하지 않는다.
목적지는 현재 로컬 개발 VM이며 최종 온프렘 설치의 인수 증빙이 아니다.

| 실제 검증 | 결과 | 범위 |
|---|---|---|
| 플랫폼 → 두 DB·두 PgBouncer | 36 PASS | Executor와 같은 네트워크 label, 비루트 임시 Pod에서 실제 제어 계정의 TLS/조회·잘못된 암호/인증서 이름 거부 16개 → 다른 caller label의 TCP 거부 4개 → 정상 재대조 16개 |
| 플랫폼 → 실행 기록 DB | 5 PASS | 전용 runtime 역할, 엄격 TLS, 테이블/스키마 최소 권한, 잘못된 암호/이름 거부와 정상 재접속. 실행 기록 수 0, DB 쓰기 0 |
| 출발지 앱 새 이미지 | 9 PASS | demo-chat #12/#13의 이미지 digest를 실제 적용하고 WebSocket 1012·재접속·인증 거부·반복 종료를 확인. 쓰기 차단 유지, 업무 쓰기 0 |
| 출발지 풀러 → 목적지 DB | TLS 3 PASS | 실제 풀러 Pod에 목적지 TLS 호스트의 hostAlias만 추가한 뒤 인증서 체인/이름 검증, 잘못된 이름 거부, 정상 재접속 |
| 출발지 다른 caller → 전용 풀러 경로 | 첫 시험 FAIL | 잘못된 label의 Pod에서도 TCP 연결됨. 정상 연결을 앞뒤에서 확인했고 시험 Job/Pod는 삭제 후 부재 확인 |

출발지 풀러의 기존 backend는 계속 출발지다. 변경은 관측한 전용 Service 주소를 목적지 인증서 이름에
연결하는 hostAlias뿐이며 ConfigMap/실행 중 설정과 다른 Deployment 사양을 보존했다. 새 Pod의 named-exec
권한은 환경 담당이 재결속했다. 생성된 Service의 Ready와 정책 YAML만으로 호출자 제한을 통과 처리하지 않는다.
첫 거부 시험 실패는 환경 담당이 EKS의 실제 network-policy 기능이 꺼진 상태임을 확인해 수정 중이다.
수정 후 동일한 정상→거부→정상 시험이 통과하기 전에는 해당 경로를 준비 완료로 표시하지 않는다.

플랫폼의 첫 CPU 100m 요청 시험은 자원 예약량 부족으로 실행되지 않았다. 실제 읽기 시험은 CPU 요청 10m로
한 번에 하나씩 실행했다. 플랫폼 노드의 실제 Executor 100m 배치 용량은 별도 미해결 조건이다.
소유한 실패/성공 시험 Job·Pod·임시 Secret·전달 키는 최종 부재 조회까지 확인했다.
자격은 인증된 암호문으로 전달하고 Pod의 Secret에서 읽었으며, 로컬 파일·SSM 입력·로그에 평문으로 남기지 않았다.

선택한 결과와 비공개 원본의 해시는 [validation.json](validation.json)에 있다.
실제 T12 정상 컷오버·ABORT_PRE_WRITE·SAFE_HOLD와 T13 MISMATCH→MATCH 영수증은 아직 없다.
