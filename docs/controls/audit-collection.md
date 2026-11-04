# T34 감사 수집과 T14 관찰 연결

T34의 완료는 양쪽 환경에서 실제 카나리 3종을 발생시켜 10분 안에 검색하고,
검색 권한이 없는 신원이 거부되는 것을 실측하는 것이다. 이 문서와 `audit_probes.py`는
그 연결을 위한 준비다. **T6·T30 실환경 수집, 권한 거부, T34 전체 완료를 대신하지 않는다.**

## 책임과 입력

| 구성 | 책임 |
|---|---|
| T6/T30 환경 소유자 | CNPG `log_connections`, pgAudit DDL, EKS/RKE2 audit 설정과 수집 경로 |
| 카나리 실행기 | 승인 범위 확인, 고유 표식, 로그인 또는 생성·삭제 성공 확인, 실행 영수증 |
| T34 검색 어댑터 | 등록된 로그 저장소·stream·시간창의 완전한 검색, 실패·권한 부족 구분 |
| T14 관찰 | 실행 ID·앱·환경·시험 결속, 의미 있는 이벤트 일치 확인, 증거 저장 |
| T19 | 만료·누락·UNKNOWN 차단과 원본/대상 비교 |

`CanaryReceipt`는 신뢰된 실행기가 만든 내부 자료형이다. JSON 업로드 API가 아니며,
`acknowledged` 값을 사용자나 에이전트가 작성하게 해서는 안 된다. 검색 클라이언트도
서버가 실제 신원과 소유권을 확인한 뒤 주입한다. 소스 ARN 문자열만으로 신원을 증명하지 않는다.

카나리는 `bravo_audit_<32자리 무작위 hex>`를 사용한다. DB `application_name`과
시험 테이블 이름, RoleBinding 이름에 같은 실행의 고유 표식을 쓴다. 실행 ID가 달라지면
이전 영수증을 재사용하지 않는다. 재시도에서 같은 카나리 실행을 다시 생성하지 않도록
실행·조회·정리 상태를 T10 작업 원장에 결속해야 한다.

## 판정

- AUD-01.1: CNPG `logger=postgres`, 관리자 신원·DB·application_name 일치,
  `error_severity=LOG`와 인증 완료 메시지의 사용자·DB·표식을 정확히 확인한다. PostgreSQL 18은 접속 승인 시 구조화된 `application_name`이 없을 수 있으므로 공식 메시지 형식도 대조한다. SQL 본문에 같은
  문구가 있는 것, 앱 사용자 로그인, 일반 앱 로그는 관리자 로그인 증거가 아니다.
- AUD-01.2: CNPG `logger=pgaudit`, SESSION/DDL/TABLE, 정확한 시험 스키마·테이블,
  CREATE TABLE과 DROP TABLE 이벤트를 모두 확인한다. statement 문자열 검색으로 대체하지 않는다.
- AUD-01.3: K8s audit v1의 ResponseComplete, 정확한 주체·namespace·RoleBinding,
  create/delete의 성공 응답을 모두 확인한다. RequestReceived·403·다른 주체·impersonation은 제외한다.
- 모든 이벤트는 등록된 stream과 실행 시간창에 속해야 한다. 중복 JSON 키, 잘린 페이지,
  수집 범위 불일치, 읽을 수 없는 로그를 PASS로 처리하지 않는다.
- 생성 성공 직후 아직 로그가 없으면 `UNKNOWN / NOT_RUN`이다. 기본 10분 창이 끝난 뒤
  완전한 검색에서 필요한 이벤트가 없으면 `absent / FAIL`이다. 로그 검색 권한이 없으면
  `UNKNOWN / OBSERVATION_DENIED`이며, 정상 거부 시험의 PASS로 바꾸지 않는다.
- 늦게 수집된 이벤트는 10분 안에 검색됐다는 근거가 아니다. 이벤트 시각과 수집 시각을
  모두 확인한다. 원본 로그 전체를 공개하지 않고 일치한 이벤트의 해시·행동·시간을 보존한다.

`AuditSearchProbe`는 한 번 조회하고 반환한다. 10분 동안 요청 스레드를 붙잡지 않는다.
T10 워커가 같은 실행의 receipt를 보존하고 제한된 재조회로 이어가야 한다. 공개 업로드 자료나
AI 응답을 `search`/`receipt_provider`로 등록해서는 안 된다.

## 출발지 CloudWatch

`CloudWatchAuditSearch`는 서버 등록 로그 그룹 ARN과 정확한 stream 목록에 대해
`FilterLogEvents`만 호출한다. `unmask=false`, 고정 표식 필터, 시간창을 사용한다.
빈 페이지에도 nextToken이 있으면 계속 읽고, 반복 토큰·페이지 예산 초과는 UNKNOWN이다.
등록되지 않은 stream으로 자동 확장하지 않는다. CNPG 재기동으로 stream이 바뀌면 등록을
다시 검증해야 하며 이전 stream에서 0건이라는 이유로 정상 판정하지 않는다.

권한은 해당 로그 그룹의 `logs:FilterLogEvents`만 기본으로 부여한다. 별도 등록 담당자가
stream을 조회하고 검증하며, 검색 워커에는 광범위한 Describe/Get/Unmask·로그 삭제 권한을
붙이지 않는다. 실제 IAM 정책의 자원 ARN과 거부 결과는 T6 계정에서 검증해야 한다.

T6의 현재 CNPG `kubectl logs` 실측은 중앙 수집기 검색의 증거와 구분한다. DB 로그가
CloudWatch로 전달되기 전에는 이 어댑터로 DB 로그 검색을 완료했다고 표시할 수 없다.

## 목적지 RKE2와 CNPG

[`rke2-audit-policy.yaml`](../../services/control-runner/templates/rke2-audit-policy.yaml)은
온프렘 user-data가 RKE2 첫 시작 전에 넣는다. Secret 본문을 담지 않는 Metadata 수준으로 권한 변경을
기록한다. 측정은 [로컬 VM 기록](../evidence/t30-rke2-audit-2026-11-04/README.md)에 있고 데모 머신과
T7 EC2에는 적용하지 않았다. 이 파일만으로 로그가 노드 밖으로 전달되지는 않는다.

실제 로그에서 확인한 주의점이 둘 있다. `pods/exec` 명령 인자는 `requestURI`에 실려 Metadata에도
남으므로 비밀을 인자로 넘기지 않는다. 거부된 생성(403)은 `objectRef.name`이 없어 이름으로 찾을 수 없다.

1. RKE2 kube-apiserver가 해당 policy와 audit 파일 경로를 사용하게 한다. 온프렘 경로는 로컬 VM에서
   확인했고 데모 머신은 재설치 뒤 다시 잰다.
2. CNPG는 접속 성공 로그·pgAudit DDL을 활성화하고 파라미터 로깅은 끈다.
3. 기존 수집기를 이용해 **목적지에서 시작하는 TLS 연결**로 저장소에 전달한다.
   별도 외부 수신 포트를 열지 않는다. writer는 쓰기 전용, verifier는 검색 전용으로 분리한다.
4. 클러스터·namespace·pod/container 신원을 수집기에서 확인한다. 앱이 임의로 같은 로그
   label을 붙이는 것으로 CNPG/K8s audit 원본을 사칭할 수 없어야 한다.
5. 회전·보존·디스크 한도·전송 재시도·수집 중단 알림을 운영 설정으로 확인한다. RKE2 값은 100MiB,
   백업 10개, 30일이고 상한은 약 1.1GiB다. 워크로드 없는 노드에서도 하루 약 380MiB가 쌓여 실제 보존은
   3일쯤이다(로컬 VM 실측). 전송 재시도와 수집 중단 알림은 남아 있다.
6. 목적지 검색 어댑터가 같은 `AuditBatch` 의미를 구현한 뒤 실측한다.

목적지 저장소는 [ADR-0016](../adr/0016-target-audit-store.md)에서 SeaweedFS Object Lock 버킷(OTLP JSON 객체, 쓰기 전용 · 검색 전용 identity, COMPLIANCE 보존)으로 제안했다(제안됨). 로컬 Docker에서 권한 분리 · 보존 잠금 · Collector `awss3` 전달을 실측했고, 실제 목적지 배포와 검색 어댑터는 남아 있다.

## 실제 완료 전 필수 시험

- 양쪽 각각 관리자 로그인·DDL 생성/삭제·RoleBinding 생성/삭제를 발생시키고 검색 시간을 기록
- 동일 표식이라도 다른 앱·DB·namespace·주체인 이벤트를 거부
- 정상 검색 역할 성공 → 다른 역할 AccessDenied → 정상 역할 재확인
- 수집기 중단, 페이지 누락, clock 차이, 10분 초과 수집을 PASS로 표시하지 않음
- 카나리 생성 후 응답 유실·재시작·정리 실패를 원장에 보존하고 무작정 재생성하지 않음
- 실패한 정리 작업은 보안 운영자에게 남기고 시험 전용 자원만 다시 처리

단위 시험은 합성 CNPG/K8s JSON과 Botocore Stubber를 사용한다. 별도 로컬 kind v0.33.0 /
Kubernetes v1.34.0 / CNPG 1.30.1 / PostgreSQL 18.4 환경에서도 다음을 실측했다.

- 관리자 접속, 시험 테이블 생성·삭제, RoleBinding 생성·삭제의 실제 로그를 이 판정기로 확인
- 고유 표식 발생부터 직접 로그 조회까지 약 0.4초; 중앙 수집 지연 측정은 아님
- 단기 ServiceAccount 토큰으로 지정 Pod 로그 읽기 성공 → 무권한 역할 403 → 읽기 역할 재확인
- PostgreSQL 로그인 시 `application_name`이 구조화 필드에 없는 실제 형식을 반영하고 회귀 시험 추가

이 시험은 localhost API를 사용하는 별도 클러스터와 비업무 DB를 사용했다. 원시 로그·토큰·
호스트 경로는 공개 산출물에 넣지 않는다. **로컬 직접 조회는 CloudWatch/RKE2 중앙 수집 또는
운영 환경의 보존·검색 권한 자격을 대신하지 않는다.** 실제 양쪽 중앙 수집·장애 시험과
읽기 전용 권한 검증이 남아 있으므로 T34는 완료 처리하지 않는다. T5 계약의 10분은 현재
시험 기준이며 실제 환경에서 재검토할 수 있다.

## 공식 형식 근거

- [CloudWatch FilterLogEvents](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html): 빈 페이지·페이지 끝·마스킹
- [CNPG pgAudit 자료형](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/pkg/management/postgres/logpipe/pgaudit.go): 구조화된 audit 필드
- [Kubernetes auditing](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/): 단계·Metadata 정책
- [pgAudit](https://github.com/pgaudit/pgaudit/blob/main/README.md): DDL 세부 항목과 운영 부하

- [PostgreSQL 18 인증 완료 로그 형식](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/utils/init/postinit.c): application_name과 TLS 필드
