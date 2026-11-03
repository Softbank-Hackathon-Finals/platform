# T10 플랫폼 DB 소비자 실측 — 2026-11-03

검토된 `PostgresPlanStore`와 `runtime_probe`를 기존 플랫폼 PostgreSQL에 연결한 독립 소비 검증이다. 인프라 담당자의 DB 구축·migration·Pod 재생성 검증 이후, 제공된 접속 계약의 runtime Secret과 CA를 제한된 Job에 읽기 전용으로 mount했다. 자격 값은 Secret 파일과 자식 프로세스 환경에서만 소비했으며 로컬 DSN 파일, 명령 인자, 출력, 공개 증빙에 넣지 않았다.

## 관측

| 단계 | UTC 관측 시각 | 결과 |
|---|---|---|
| `exercise` | 11:13:33.583230 | 19개 boolean 모두 true. PostgreSQL 18.4, verify-full, runtime 권한·소유권, 동시 등록/전이 재시도/append, rollback, tenant·cursor 및 변경 거부 |
| 별도 프로세스 `verify` | 11:13:34.896809 | 14개 boolean 모두 true. 같은 checkpoint로 상태·이벤트 4개·watermark·원래 명령 영수증 보존 및 권한 경계 재확인 |
| wrapper 완료 | 11:13:34.966458 | 서로 다른 두 자식 프로세스, 동일 checkpoint hash, 고정 소비자 소스 6개 hash 확인. 기존 owner baseline의 snapshot·receipt hash도 읽기 조회로 일치 |

19개와 14개는 각 단계의 boolean 수다. 두 단계가 같은 역할·보존 조건 일부를 재검사하므로 서로 다른 33개 기능을 뜻하지 않는다. 원시 결과에는 이벤트 수 `4`와 서버 버전 `180004`가 별도 숫자로 있다. Python 3.12.16과 psycopg 3.3.7을 사용했다.

두 CLI 실행은 같은 제한 Job 안의 서로 다른 프로세스다. `exercise` 종료 뒤 새 프로세스를 시작했고, `verify`가 상태나 영수증을 재생성하지 않는 경로로 확인했다. 합성 plan 하나는 의도적으로 남겼다. checkpoint에는 합성 plan 식별자와 timestamp만 있고 비밀은 없다.

## 실행 소스와 증거

- [runtime-readback.json](runtime-readback.json): 성공한 클라우드 조회 응답의 bounded `StandardOutputContent` 바이트 그대로다. 전체 공급자 응답의 사본은 아니다. 합성 plan 식별자 외 실제 리소스 이름·주소·계정·자격은 포함하지 않는다.
- [execution-source.json](execution-source.json): 실행 당시 커밋 `e6c06320b07ffaeeb0549803709d97355d37aee6`, 소비자 파일·SQL·lockfile, 고정 이미지·bundle·operator·wrapper hash. 후속 문서 커밋을 실행 소스로 소급하지 않는다.
- [cleanup-readback.json](cleanup-readback.json): 11:17:36.671406Z에 이번 실행의 완료 Job과 정확히 같은 소스 ConfigMap만 삭제한 뒤 부재를 확인했다. DB와 기존 NetworkPolicy는 수정하지 않았다.
- [receipt-manifest.json](receipt-manifest.json): 비공개 공급자/command 응답과 공개 bounded stdout의 hash, 동일 바이트 확인 및 checkpoint 결속. 원본 응답과 Job 포장용 wrapper/operator는 권한 있는 검토자에게 제공하는 비공개 기록으로 보존했다.

Job은 기존 runtime ServiceAccount, 토큰 자동 mount 금지, root가 아닌 사용자, 읽기 전용 root filesystem, 제한 자원, Secret/CA 읽기 전용 mount를 사용했다. 기존 검증 NetworkPolicy가 제공된 계약과 같은지 먼저 읽어 확인했고 수정하지 않았다. 새 ConfigMap과 검증 Job만 생성했다. 의존성은 기존 검증 경로의 hash 고정 wheel 목록으로 설치했다. 실제 DB 재시작, DDL, 역할 부여, 기존 Terraform state 변경은 하지 않았다.

기존 baseline 비교는 인프라 담당자가 먼저 수행한 DB Pod 재생성의 **후속 읽기**다. 이 소비자가 재생성 이전 상태를 직접 관측하거나 DB Pod를 삭제한 것이 아니다. baseline의 원래 snapshot·receipt hash와 이 실행의 읽기 결과가 일치한다는 것까지만 확인했다.

## 완료 범위

이 결과는 실제 플랫폼 DB에서 합성 plan의 저장 계약, 재접속, 제한된 runtime 권한이 동작한다는 근거다. DB HA·백업 복원, 장기간 장애 복구, 실제 collector 수집 완료, HTTP 사용자 인증, 전체 상태 머신·승인·Executor, T33 전체 이식, 플랫폼 API 운영 배포를 입증하지 않는다. T10 전체 완료나 새로운 DB 운영 정책 채택을 표시하지 않는다.

반복 가능한 CLI 절차와 로컬 실패 경계 시험은 [런타임 연결 계약](../../contracts/T10-postgres-runtime-verification.md)을 따른다.
