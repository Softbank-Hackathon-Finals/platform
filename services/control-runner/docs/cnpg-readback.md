# CNPG 백업 완료 읽기 검증

`CNPGBackupReader`는 인증된 Kubernetes GET으로 등록된 Cluster, ObjectStore, Backup을
대조한 뒤 `CompletedBackup`을 만든다. 백업을 시작하거나, 복원 데이터가 정상이라고
판정하는 기능은 아니다. DR-01.1은 이후 새 복원 DB의 실제 조회까지 필요하다.

## 신뢰 경계

`RequestBinding`은 사용자 JSON이 아니라 실행 원장이 보존한 요청 영수증이다.

1. 실행기가 앱·환경·클러스터 신원, 승인 범위와 읽기/쓰기 권한을 확인한다.
2. 백업 **요청 전** Cluster와 ObjectStore의 UID·generation·spec 지문을 보존한다.
3. 백업을 한 번 요청하고 인증된 create 응답의 Backup UID와 요청 시각을 보존한다.
   응답이 불명확하면 실행 원장의 재조정 절차로 확인한다. 이 읽기 검증기는 재생성하지 않는다.
4. 같은 서버 실행 문맥에서 검증기를 호출한다. 백업 완료 후 현재 설정으로 2번의 영수증을
   사후 생성하면 과거 설정의 증거가 되지 않는다.

GET 클라이언트는 등록된 Kubernetes 서버 인증서와 신원을 검증하고 요청마다 제한시간을
적용해야 한다. 최소 RBAC는 해당 namespace의 이름이 고정된 세 자원의 `get`이다.
`list`, `watch`, Secret 읽기, Pod exec, create/delete는 이 검증기에 필요하지 않다.
전송 단계의 최대 응답 크기·전체 취소는 호출자가 강제한다. 검증기는 객체별 256 KiB와
전체 읽기 30초를 확인하지만, 멈춘 동기 클라이언트를 자체 강제 종료하지는 않는다.

## 거부 조건

- 앱·환경·실행 문맥 불일치, 미래 또는 24시간보다 오래된 요청
- 다른 namespace/name/UID, 삭제 중 자원, 잘못된 API/kind
- 요청 전과 다른 Cluster/ObjectStore generation 또는 spec
- WAL archiver와 ObjectStore 연결 불일치, 백업 요청 spec의 변경
- `completed`가 아닌 상태, 오류 상태, 다른 플러그인 또는 원본 clusterUID
- `latest` 등 부정확한 backup ID, 누락·역전·미래의 완료 시각
- 전후 읽기 사이 자원 교체, 설정·백업 상태 변화, 시간 역행
- 권한 부족·연결 오류·시간 초과·증거 저장 실패

이 경우 완료 영수증을 반환하지 않는다. `Runner`에 연결하면 오류는 UNKNOWN으로 남고,
정상 복원 데이터가 불일치한 FAIL과 구분된다. 두 번의 읽기는 API 전체의 원자적 스냅샷을
제공하지 않는다. 조회 이후 변경은 실행 직전 게이트에서 재확인해야 한다.

`generation`은 spec을 변경했다가 되돌린 경우도 탐지한다. Secret 참조의 실제 값 회전,
스토리지 객체 손상·삭제, 컨트롤러 관리자에 의한 상태 위조를 검증하는 기능은 아니다.
컨트롤러 상태를 쓸 수 있는 주체는 신뢰 경계 안에 있으며 해당 권한을 제한해야 한다.

## 증거와 후속 복원

증거에는 앱·환경·run ID, UID 해시, source/store 결속 해시, 완료 상태 해시와 수집 시각을
남긴다. Secret 값, DB 행, API 원문, 버킷 주소는 남기지 않는다. 실제 영수증과 원문은
접근 통제된 원장에 별도 보존한다. 해시는 출처 인증이나 서명을 대신하지 않는다.

`archive_binding_sha256`은 source/store 요청 전 지문으로 계산한다. 복원 실행기는 같은
결속과 정확한 backup ID를 이용하며, 읽기 전용 저장소 자격을 별도로 쓴다. 그 뒤
`RestoreProbe`가 새 클러스터·표식·행 수를 검증한다. **컨트롤러 completed만으로
백업 가독성, RPO/RTO, 전체 데이터 무결성 또는 재해 복구 완료를 주장하지 않는다.**

## 검증 범위

[로컬 실제 읽기 검증](../../../docs/evidence/t35-cnpg-readback-2026-11-03/README.md)은
기존 전용 kind 환경에서 새 백업 하나를 생성해 수행했다. 팀 AWS/RKE2 등록·원장 연결,
최소 권한 서비스 계정, 운영 저장소 보존 정책은 별도 통합 검증 사항이다.

CNPG 1.30.1 / Barman Cloud plugin 0.15.1에서 확인한 API 응답을 기준으로 한다.
향후 버전이 상태 필드나 기본 spec을 바꾸면 자격 시험 후 명시적으로 지원한다.

## 복원 대상과 실제 SQL 조회의 결속

`CNPGRestoreReader`는 별도 `RestoreRequestBinding`과 인증된 GET·DB 연결 factory를 받는다.
실행기가 정확한 백업 ID와 읽기 전용 아카이브 경로를 검증하고 복원을 한 번 생성한 뒤,
**생성 응답의** UID·기본값 적용 후 spec 지문 및 생성 전에 읽은 ObjectStore 지문을 기록한다.
복원이 끝난 다음 지문을 만들어 과거 결속이 있었다고 주장하면 안 된다.

- `CompletedBackup`과 앱·환경·run 문맥이 동일한 요청만 수용한다.
- 새 Cluster UID, generation 1, 정확한 `backupID`와 `targetImmediate`, 원본 이름과
  reader ObjectStore, 단일 인스턴스 및 archive writer 부재를 확인한다.
- Ready/Initialized와 현재 primary를 확인하고 전용 SQL 연결에서 표식·행 수를 조회한다.
- SQL host·DB 이름, `verify-full`과 실제 TLS 사용을 검증한다.
- 조회 뒤 Cluster/ObjectStore 지문·primary·Ready 시각을 다시 확인한다.
- 대상 교체·설정 변경·권한 부족·시간 초과·증거 저장 실패면 성공 영수증을 반환하지 않는다.
- 데이터가 정상 조회됐지만 표식이 없거나 다르면 `RestoreProbe`가 FAIL로 판정한다.
  관측 실패는 Runner의 UNKNOWN이며 같은 의미가 아니다.

**연결 factory의 의무:** 신뢰된 등록에서 얻은 대상 CA를 고정하고 실제 연결을 해당
Cluster의 primary에 결속해야 한다. DNS 이름 검사만으로 자원 소유권이 증명되지는 않는다.
이름이 같은 원본/다른 대상의 CA를 임의로 사용하거나 사용자 입력 URL을 받아서는 안 된다.
로컬 실험에서는 인증된 Pod ownerReference UID를 확인한 후 그 Pod로 터널링했다.
운영 factory에는 전송 기한·취소와 등록 신원·CA 정책이 필요하다.

**아카이브 factory의 의무:** reader ObjectStore가 완료 백업과 같은 원본 아카이브를
읽는지, 읽기 전용 역할이 실제 강제되는지 실행 전에 확인해 등록한다.
이 검증기는 Secret 내용이나 저장소 IAM 정책을 새로 읽지 않는다.

이 경로는 단일 인스턴스의 정확한 base-backup 복원 시험이다. HA 복원·PITR·전체 테이블
정합성·운영 RTO/SLO 자격을 대신하지 않는다. 두 API 조회도 분산 원자적 스냅샷이 아니며,
계속 유지되는 실행권과 실행 직전 재검증은 오케스트레이터 책임이다.
