# T30 개발 목적지와 T8 복제 준비

이 경로는 **별도 로컬 개발 VM**의 새 `target-dev` 영역을 위한 것이다.
T2의 기존 VM, AWS 플랫폼 RKE2의 상태 DB, 실제 온프렘 데모 머신과 구별한다.
새 VM의 UUID·프로세스 시작 시각·SSH 접속 계약과 실제 Kubernetes 신원을 먼저
확인한 운영자가 사용한다. 렌더러는 접속·배포·DB 변경을 실행하지 않는다.

## 빈 DB와 스키마 적용

공통 RKE2 `infra/shared/rke2/bootstrap.sh`를 수정 없이 사용한다. 개발 VM은
고정 앱 이미지의 amd64 실행을 위해 x86_64 QEMU/TCG를 사용한다. 에뮬레이션에서
측정한 지연을 실제 데모 머신의 성능으로 사용하지 않는다.

1. 실제 노드가 Ready이고 기존 CNPG/operator/namespace/PV가 없는 새 환경인지 확인한다.
2. 새 VM에 `/var/lib/bravo-target-dev/postgres`를 UID/GID 26 소유로 만든다.
   기존 데이터가 있으면 적용하지 않는다. DB 전용 local PV는 8Gi, Retain이며 HA·백업을 제공하지 않는다.
3. `target-dev` namespace와 `target-app-db`, `target-replication` Secret을 준비한다.
   Secret은 `kubernetes.io/basic-auth` 타입의 `username`, `password` 필드다.
   생성한 값은 메모리에서 Kubernetes 입력으로 전달하고 파일·출력·PR에 남기지 않는다.
4. 공식 cloudnative-pg chart 0.29.1과 `cnpg-values.yaml`로 operator 1.30.1을 설치한다.
   `cnpg-system`만 operator 용도로 사용하며 감시 범위는 `target-dev`다.
5. `python3 render.py database --node-name <확인한-노드>`의 결과를 검토·적용한다.
   `target-db`가 Ready이고 애플리케이션 테이블이 없는 것을 확인한다.
6. `python3 render.py migration`의 결과를 별도로 적용한다. 이 Job이 성공한 다음에만
   구독을 준비한다. 기존 테이블이 있으면 전체 트랜잭션이 실패하므로 충돌을 숨기지 않는다.

`schema.sql`은 고정 demo-chat 소스 `ce5727eb3e3d2355077afdaadcb1a722aba9c7cd`의
두 테이블에 대응하며 대상 권한과 `target_chat` publication을 추가한다.
논리 복제는 스키마와 시퀀스를 옮기지 않으므로 이 Job과 전환 시 시퀀스 보정은 별도다.

## 닫힌 상태에서 시작

- `chat_app`은 NOLOGIN이다. 목적지 업무 쓰기를 열지 않는다.
- `chat_replicator`는 SCRAM 로그인·논리 복제와 두 테이블 SELECT만 사용한다.
  물리 복제 전용 HBA 규칙은 추가하지 않는다.
- DB는 TLS만 허용한다. 마이그레이션 Job도 `verify-full`과 대상 CA를 사용한다.
- 감사는 접속·DDL·역할 변경을 수집하고 파라미터 값 로깅을 끈다.
- 출발지/목적지 endpoint와 환경 신원, 단일 쓰기 조건이 확정되기 전에는 구독·슬롯을 생성하지 않는다.

## 아직 별도인 통합

이 렌더는 Tailscale 경로·ACL, PgBouncer의 출발지 연결, 논리 구독, 첨부 S3 동기화,
앱 배포를 완성하지 않는다. T8의 실제 왕복 지연·재연결·단일 쓰기·해시 근거는
실행 시점의 코드와 두 환경 신원에 결속해야 한다. 플랫폼 DB를 목적지 DB로 사용하지 않는다.

고정 앱 소스 ce5727e는 시작 시 AWS Secrets Manager SDK를 필수로 호출한다.
OpenBao/ESO의 환경 변수만으로 같은 이미지가 AWS 없이 시작한다고 주장하지 않는다.
OCI 원본 바이트 수입/패키징 확인과 실제 목적지 업무 기동을 구별하며, 앱 의존성 변경은
T11/T15의 검증·승인 경계에 따라 처리한다. 개인키·AWS 자격을 이미지에 넣지 않는다.

## 공식 계약

- [CNPG 1.30.1 논리 복제](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/logical_replication.md)
- [PostgreSQL 18 구독](https://www.postgresql.org/docs/18/sql-createsubscription.html)
- [RKE2 요구 사항](https://docs.rke2.io/install/requirements)

T2/T6/T8/T9/T30 전체 완료나 실제 데모 머신의 준비 완료는 이 문서만으로 판정하지 않는다.
