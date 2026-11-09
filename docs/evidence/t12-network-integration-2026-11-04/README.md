# T6/T30 기반 준비: 실제 제어 경로와 출발지 풀러의 목적지 이름 해석

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
| 출발지 다른 caller → 전용 풀러 경로 | 수정 후 PASS | 실제 풀러 TLS → 다른 label의 직접 ClusterIP TCP 거부 → 실제 풀러 TLS. 임시 Job/Pod 부재 확인 |
| 다른 namespace의 같은 pooler label → 전용 경로 | PASS | 호출자 egress 정책 없는 별도 namespace에서 동일 label로 직접 접속해 거부 확인. 앞뒤 실제 풀러 TLS 정상, 임시 Job/Pod 부재 확인 |

출발지 풀러의 기존 backend는 계속 출발지다. 변경은 관측한 전용 Service 주소를 목적지 인증서 이름에
연결하는 hostAlias뿐이며 ConfigMap/실행 중 설정과 다른 Deployment 사양을 보존했다. 새 Pod의 named-exec
권한은 환경 담당이 재결속했다. 생성된 Service의 Ready와 정책 YAML만으로 호출자 제한을 통과 처리하지 않는다.
첫 거부 시험은 잘못된 label에서도 TCP가 연결되어 실패했다. 환경 담당이 EKS의 실제 network-policy
기능이 꺼진 상태를 확인해 공식 add-on 설정을 수정했다. 실제 node agent 활성화와 PolicyEndpoint의
풀러/프록시 주소 결속을 확인한 뒤 위 두 시험을 각각 다시 수행해 통과했다. 각 호출자 namespace에는
egress 정책이 없어 출발지 차단이 거부 결과를 대신하지 않는다. 최초 실패 기록도 보존했다.
CNI 변경과 기존 IAM 역할 보존·복원 근거는 별도 [#142](https://github.com/Softbank-Hackathon-Finals/platform/pull/142)에 있다.

플랫폼의 첫 CPU 100m 요청 시험은 자원 예약량 부족으로 실행되지 않았다. 실제 읽기 시험은 CPU 요청 10m로
한 번에 하나씩 실행했다. 플랫폼 노드의 실제 Executor 100m 배치 용량은 별도 미해결 조건이다.
소유한 실패/성공 시험 Job·Pod·임시 Secret·전달 키는 최종 부재 조회까지 확인했다.
자격은 인증된 암호문으로 전달하고 Pod의 Secret에서 읽었으며, 로컬 파일·SSM 입력·로그에 평문으로 남기지 않았다.

선택한 결과와 비공개 원본의 해시는 [validation.json](validation.json)에 있다.
실제 T12 정상 컷오버·ABORT_PRE_WRITE·SAFE_HOLD와 T13 MISMATCH→MATCH 영수증은 아직 없다.

담당 경계: 새 호스트 이름 패치 함수와 시험은 T6 인프라 디렉터리에만 둔다. 이 후속 PR은
`services/cutover-executor`를 변경하지 않는다. 이미 병합된 #134는 보존하고, 비교기·1012 호출·실패 처리·
Executor 배포 및 실제 컷오버는 공식 T12 담당과 인수 범위를 정해 이어간다. 로컬 채팅 분담은 그 합의를 대신하지 않는다.
