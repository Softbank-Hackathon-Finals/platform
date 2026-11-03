# T35 CNPG 백업·복원 자격

T35는 백업 API의 성공만으로 DR-01.1을 통과시키지 않는다. 백업 직전의 일관된 표식과
그 이전 행 수를 기록하고, **새 클러스터로 실제 복원한 DB**에서 같은 내용을 읽어야 한다.
이 경로는 T5의 제한된 append-only 시험을 구현하며 임의 DB 전체의 무결성 보장은 아니다.

## 구현과 소유권

- `bravo_controls.cnpg_backup`: CNPG/Barman ObjectStore, Backup, 새 복원 Cluster를 생성하는
  순수 함수. 실행·승인·Terraform 적용을 하지 않는다.
- `bravo_controls.backup_restore`: 실행·백업·복원 영수증의 결속과 신선도를 확인하고
  실제 복원 표식·행 수를 비교하는 T14 DR-01.1 관찰기.
- `bravo_controls.postgres_restore`: `postgres` extra의 psycopg를 사용한 고정
  `public.messages` 조회. 전용 유휴 연결, `verify-full`, 읽기 전용 반복 가능 읽기
  트랜잭션, 쿼리·잠금 시간 제한을 요구하며 원문 대신 표식 해시를 반환한다.
- T6/T30 담당자: 원본 CNPG, 오퍼레이터/플러그인 버전, 서비스 계정, 네트워크·저장소 적용.
- T9 담당자: SeaweedFS TLS·인증·접근 통제와 수명 관리.
- T10 실행기: 승인 범위·소유권·리소스 UID, 작업 접수·효과 원장·복구·정리.

현재 함수의 입력은 **신뢰된 서버 내부 등록과 실행 영수증**이다. 모델이나 사용자가 제출한
`source_uid`, 백업 ID, 해시, 표식 자료를 진실로 받아들이는 API가 아니다. 해시는 서명이나
권한 증명이 아니므로 영수증의 작성자·보존·접근 권한을 실행 계층에서 확인해야 한다.

## 백업 저장소 구성

1. CNPG 1.30.1 및 Barman Cloud 플러그인 0.15.1을 첫 호환 시험 대상으로 삼는다.
   기존 CNPG 내장 백업 설정과 섞지 않고 `method: plugin`을 사용한다.
2. 출발지 S3는 등록된 workload IAM으로 접속한다. 생성 함수는 장기 키와 임의 endpoint를
   허용하지 않는다. namespace/ServiceAccount/OIDC 주체 결속은 T6가 검증한다.
3. 목적지 SeaweedFS는 HTTPS·CA와 Secret 참조를 요구한다. endpoint 문자열 검사는 DNS,
   대상 소유권, egress 허용을 대신하지 않는다. 서버 등록에서 이를 확인한다.
4. 버킷 루트가 아닌 앱별 명시적 prefix를 사용한다. 복원 읽기 역할은 백업 쓰기 역할과
   분리한다. 앱/Executor에 백업 자격을 주지 않는다.
5. 삭제/보존 정책은 별도 관리 주체가 맡는다. 자동 retentionPolicy는 생성하지 않는다.
   `s3_delete_guard`는 앱·Executor 역할의 객체/버전/버킷 삭제를 막는 **병합 검토용** 정책이다.
   기존 bucket policy를 이 문서의 출력으로 통째로 덮어쓰지 않는다.

쓰기 역할에는 필요한 prefix의 Put/Get/List와 multipart 작업만, 복원 역할에는 Get/List만
부여한다. 앱·Executor가 bucket policy·IAM·KMS 정책을 바꿔 삭제 제한을 해제할 수 없어야 한다.
버전 관리, 암호화와 KMS 키 복원 가능성, 보존 기간, 복구 계정, lifecycle은 실환경 적용 전
정한다. **DeleteObject 거부만으로 불변 백업·랜섬웨어 방어를 완료했다고 주장하지 않는다.**
SeaweedFS의 정책 지원도 실제 버전에서 거부 시험을 통과해야 하며 S3라는 이름만으로 동등
판정하지 않는다. 백업 파일에 고객 데이터·역할 인증정보가 포함될 수 있으므로 원본과 같은
데이터 분류·보관 위치·접근·폐기 기준을 적용한다.

## 표식과 데이터 경계

T5 DR-01.1의 `id ≤ 표식` 비교에는 다음 전제가 필요하다.

- 표식보다 먼저 ID를 받았으나 아직 커밋하지 않은 쓰기가 남아 있지 않음
- 비교 대상 행에 UPDATE·DELETE·TRUNCATE가 시험 동안 일어나지 않음
- 표식 생성·커밋 성공을 확인한 다음 백업을 시작함

시퀀스의 nextval만으로 이 전제를 입증할 수 없다. 제한된 채팅 시험은 테이블 쓰기 잠금을
취득해 이전 변경을 drain한 뒤 표식을 커밋하고, 해당 범위가 append-only인지 검증한다.
잠금 획득은 시간 제한을 두고 업무 영향에 대한 승인을 받는다. 확인할 수 없으면
`MarkerBoundary`를 만들지 않고 UNKNOWN으로 남긴다. 일반 앱에는 일관된 스냅샷과 업무
식별자·내용 비교를 별도로 설계해야 한다.

표식·행 수만으로 비표식 행의 내용 변조, 첨부파일, 시퀀스·권한·트리거 전체를 검증하지
않는다. 해당 항목은 데이터 이전 무결성/별도 복구 자격에서 다룬다.

## 실행 순서와 판정

1. 승인된 앱·환경·DB·source UID와 백업 저장소 결속을 확인한다.
2. 표식과 행 수, 전제 검증 근거를 보존한다. 원문 메시지·비밀값은 보고서에 넣지 않는다.
3. 고유 이름으로 Backup을 생성하고 요청 직전 시각·UID를 기록한다. 공급자의 startedAt은 초 단위일 수 있으므로 요청 시각과 구분한다. 표식 커밋 ≤ 실제 요청 시각을 확인하며, 공급자 시작 시각이 요청 시각의 초 단위 하한보다 이르면 거부한다. 응답 유실 시 같은 효과를 무작정
   재실행하지 않고 이름·UID·소유권을 읽어 재조정한다.
4. Backup `completed` 및 보관 상태를 확인해 정확한 backup ID와 시각을 보존한다.
5. 별도 빈 복원 클러스터를 생성한다. `latest` 대신 정확한 backup ID와 PostgreSQL 버전 태그+digest를 선택하고
   `targetImmediate`로 해당 백업의 일관된 상태까지 복구한다. 백업 완료보다 이전에 생성됐거나
   원본과 동일한 UID를 가진 클러스터는 복원 증거로 쓰지 않는다.
6. 생성 후 Ready만 보지 않고 실제 복원 DB에서 표식과 행 수를 새로 읽는다.
7. 같은 실행·앱·DB·원본·백업·저장소인지 검사한 다음 일치하면 success, 정상 조회에서
   불일치하면 failure다. 조회 권한 부족·오류·불명확한 출처·오래된 결과는 UNKNOWN이다.
8. 증거 저장이 실패하면 성공을 반환하지 않는다. 정리는 별도 승인된 효과로 수행한다.

복원 클러스터에는 원본 archive로 쓰는 WAL archiver를 붙이지 않는다. source/target의
이름뿐 아니라 실제 UID와 새 PVC·namespace 소유권을 확인해야 한다. **생성 전 필수 조건:**
업무 라우트에 연결되지 않은 복원 전용 영역, 실제로 강제되는 NetworkPolicy/방화벽,
검증기·운영자만 접속 가능한 DB, 제한된 읽기용 저장소 자격이다. Kubernetes 명세만
생성했다고 격리·암호화·삭제 제한이 적용된 것으로 보지 않는다.

DB 연결 생성자는 등록된 대상의 UID·호스트·CA·DB 이름을 결속하고, 연결 시간 제한과
전체 작업 제한을 설정한다. 서버의 statement_timeout만으로 네트워크 단절 때 클라이언트의
종료 시간을 보장하지 않는다. 조회 역할은 해당 시험 테이블의 SELECT 권한으로 제한하며
앱 연결 풀이나 운영 관리자 연결을 재사용하지 않는다.

## 수행한 로컬 통합 검증

2026-11-03, 이번 작업 전용 kind Kubernetes 1.34.0에서 CNPG 1.30.1,
Barman 플러그인 0.15.1, PostgreSQL 18.4, SeaweedFS 4.52를 사용했다.
이 결과는 팀 AWS·RKE2 적용 완료와 구분한다.

| 시험 | 결과 | 범위 |
|---|---|---|
| HTTPS 저장소에 실제 백업 후 정확한 backup ID로 새 CNPG 복원 | 통과 | 버전 태그와 digest를 고정한 이미지, 새로운 클러스터 UID |
| 실제 복원 DB에서 표식 내용 해시·표식까지의 행 수 비교 | 통과 | 시험 데이터 3행, 원본 쓰기 경계를 통제한 append-only 경로 |
| 복원 DB의 TLS 조회 | 통과 | psycopg 3.3.7, `verify-full`; 별도 SELECT 전용 시험 역할 |
| 검증 역할의 쓰기 및 SELECT 권한 회수 | 거부 | INSERT 거부, SELECT 회수 후 권한 오류, 재부여 후 조회 성공 |
| 존재하지 않는 표식 | 성공으로 판정하지 않음 | 실제 SQL 조회 결과를 확인 |
| 잘못된 CA 및 호스트 이름 | 각각 거부 | 별도 연결에서 인증서·이름 검증 오류를 확인; 연결 실패 일반을 합격으로 세지 않음 |
| 시험 객체 삭제 | 4개 시험 역할 모두 거부 | SeaweedFS의 앱·Executor·백업 쓰기·읽기 역할, 관리자 읽기 재확인 |
| 복원 DB의 원본 archive 쓰기 | 구성에서 배제 | WAL archiver 없음, 저장소 읽기 전용 자격; 공격성 쓰기 시험은 별도 |

로컬 인증서는 시험 CA이며 네트워크 정책 강제, 저장 시 암호화, HA, Object Lock,
AWS IAM 삭제 거부, 팀 로그 수집 경로는 이 시험으로 입증하지 않았다. 검증 역할의 시험도
로컬 복원 DB에 한정하며 팀 환경의 인증·권한 운영 자격을 대신하지 않는다.
원문 로그·자격·리소스 식별자는 비공개로 보존하고 공개 보고에는 범위와 결과만 기록한다.

## 완료 전 실환경 검증

- 양쪽 S3/SeaweedFS에 백업 → 새 CNPG 복원 → 실제 표식·행 수 일치
- 백업 진행 실패, WAL 누락, 잘못된 backup ID, 저장소 읽기 권한 회수, CA 오류
- 앱/Executor의 시험 객체 삭제 거부 및 관리자 읽기 재확인; 실백업 삭제 시도 금지
- 복원 Pod가 원본 archive에 쓰지 못하고 업무 endpoint에 연결되지 않음
- 표식 이후 신규 행이 생겨도 비교 범위 유지; 이전 트랜잭션 미완료이면 UNKNOWN
- 재시작·응답 유실·정리 실패가 원장에 남고 업무 원본은 보존됨

이 목록과 실측 결과가 모두 충족되기 전에는 T35 이슈를 닫지 않는다. 단위 시험, 로컬 실제
복원, 팀 AWS/RKE2 운영 경로 자격을 별도 표기한다.

## 공식 자료

- [Barman 플러그인 사용과 복원](https://cloudnative-pg.io/plugin-barman-cloud/docs/usage/)
- [S3/호환 저장소 인증·CA·수명 관리](https://cloudnative-pg.io/plugin-barman-cloud/docs/object_stores/)
- [CNPG API 계약](https://cloudnative-pg.io/docs/current/cloudnative-pg.v1/)
- [SeaweedFS 버킷 정책](https://github.com/seaweedfs/seaweedfs/wiki/S3-Bucket-Policies)

## 11/4 최초 복구 WAL 누락 추가 시험

기존 작업 전용 kind/CNPG에서 정상 백업의 base 파일 두 개만 새로운 archive prefix에 복사했다.
원본 WAL은 수정·삭제하지 않았다. Barman이 해당 백업의 `begin_wal` 부재를 명시하고,
별도 복원 프로세스가 오류 종료했음을 UID·spec·소유권과 함께 확인했다.
원본 백업 객체의 ETag·크기, 원본 UID·spec·Ready, 기존 정상 복원본 Ready는 유지됐다.

시험 Cluster·ObjectStore만 생성 UID 조건으로 정리했다. 기존 삭제 제한을 해제하지 않아
복사한 두 개의 base 파일은 로컬 시험 저장소에 보존한다. 원문과 생성 영수증은 비공개다.
[공개 결과](../evidence/t35-wal-loss-2026-11-04/README.md)는 정상 복원 PASS를 대신하지 않는다.
팀 AWS/RKE2, 중간 WAL 손상, PITR, 복구 시간 보장은 별도다.
