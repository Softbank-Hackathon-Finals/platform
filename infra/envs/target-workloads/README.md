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

각 렌더의 적용만으로 PgBouncer의 출발지 연결, 논리 구독, 첨부 S3 동기화,
앱 배포가 완성되지 않는다. T8의 실제 왕복 지연·재연결·단일 쓰기·해시 근거는
실행 시점의 코드와 두 환경 신원에 결속해야 한다. 플랫폼 DB를 목적지 DB로 사용하지 않는다.

고정 앱 소스 ce5727e는 시작 시 AWS Secrets Manager SDK를 필수로 호출한다.
OpenBao/ESO의 환경 변수만으로 같은 이미지가 AWS 없이 시작한다고 주장하지 않는다.
OCI 원본 바이트 수입/패키징 확인과 실제 목적지 업무 기동을 구별하며, 앱 의존성 변경은
T11/T15의 검증·승인 경계에 따라 처리한다. 개인키·AWS 자격을 이미지에 넣지 않는다.

## 분리된 네트워크 경로

`network.py values --side source|target`은 공식 Tailscale operator chart 1.104.4용
값을 출력한다. chart SHA-256과 operator/proxy amd64 이미지 digest는 코드에 고정한다.
두 클러스터의 새 `tailscale-system` namespace에만 설치하고, 기존 operator/CRD가
있으면 소유자를 확인하기 전 적용하지 않는다. 프록시의 네트워크 기능 때문에 이
namespace만 Pod Security `privileged`를 사용한다. API server proxy, subnet route,
공개 ingress는 사용하지 않는다.

OAuth 값은 렌더에 없으며 기존 `operator-oauth` Secret의 `client_id`, `client_secret`
키를 참조한다. 승인된 계정의 별도 source/target 서비스 자격을 메모리에서 Kubernetes
표준 입력으로 전달한다. Helm 인수·values·로컬 파일·출력에 값을 넣지 않는다.
각 operator 태그가 자기 쪽 프록시 태그만 소유하며, 계정 관리자가 확인한 ACL은 다음
TCP 5432 경로만 추가한다. 기존 광역 허용 규칙이 이 제한을 우회하는지도 별도로 확인한다.

| 연결 주체 태그 | 목적지 태그 | PostgreSQL 인증 계정 |
| --- | --- | --- |
| `bravo-target-replication` | `bravo-source-db` | `chat_replicator` |
| `bravo-source-replication` | `bravo-target-db` | `chat_replicator` |
| `bravo-target-pooler` | `bravo-source-db` | `chat_app` |

`network.py ingress`는 기존 CNPG 서비스의 primary selector와 일치하는 별도 DB
서비스를 만든다. 실제 등록된 상대 DB의 전체 MagicDNS 이름을 확인한 다음에만
`network.py egress --peer-fqdn ...`를 사용한다. 역할별로 별도의 단일 replica
ProxyGroup을 사용하며 데모용 HA를 주장하지 않는다. Tailnet 태그가 허용하는 것은
TCP 연결이고, SCRAM 역할·테이블 권한·TLS 서버 신원 검증은 별도의 필수 조건이다.

논리 복제의 libpq `host`에는 기존 인증서 DNS 이름, `hostaddr`에는 검증한 egress
주소를 사용한다. PgBouncer는 `hostaddr`를 지원한다고 가정하지 않고 새 Pod의
`hostAliases`로 그 인증서 DNS 이름을 확인된 egress 주소에 결속한다. CA와
`verify-full`은 유지한다. 실제 Service UID/주소가 바뀌면 이전 결속을 재사용하지 않는다.
실제 생성된 프록시의 Pod/포트가 확인되면 클러스터 안의 호출자도 해당 DB/풀러로
제한하고, 허용 연결 및 잘못된 역할·CA·호스트·물리 복제 거절을 검증한다.

## 개발용 첨부 저장소

`objects.py --node-name ...`는 SeaweedFS 4.52의 고정 amd64 이미지로 단일 인스턴스를
렌더한다. DB와 별개의 `/var/lib/bravo-target-dev/objects` 경로를 UID/GID 1000으로
준비하고 8Gi Retain PV를 사용한다. 표기 용량은 파일시스템 할당량이 아니므로 합성
객체의 크기를 제한한다. 내부 master/filer/volume은 loopback에만 바인딩하고 S3만
8333으로 제공한다. 설정 없는 익명 기동을 막기 위해 `target-seaweedfs-auth` Secret의
`s3.json`은 필수이며, 값은 운영자가 메모리에서 생성·전달한다.
volume 슬롯은 8개, 개별 volume 크기 한도는 128MB다. 기본 metadata collection이
먼저 만든 4개 volume과 별도 S3 bucket collection에 필요한 여유를 구분한다.
새 bucket에 쓸 volume이 없으면 metadata HEAD/CREATE만 성공하고 객체 PUT는 실패할
수 있으므로 topology와 실제 volume collection을 확인한다. 용량 부족을 고칠 때
기존 volume/PV/버킷을 삭제하거나 인증 권한을 넓히지 않는다.

정확한 `demo-chat-attachments` 버킷에만 접근하는 bootstrap/client 두 identity를
분리한다. bootstrap은 해당 버킷 Admin/Read/Write/List/Tagging, client는 Admin을
제외한 네 권한을 사용한다. 익명 identity와 와일드카드 권한은 추가하지 않는다.
`target-object-client` Secret에는 로컬 S3 서명용 두 키만 둔다. 이 값은 AWS 키가 아니며
AWS SDK의 기본 인증 체인·session token을 재사용하지 않는다.

클라이언트는 SigV4, path style, `us-east-1`, 명시적 로컬 자격과 정확한 서비스 주소를
사용한다. NetworkPolicy는 같은 namespace의 `bravo-object-client=true` Pod만 허용한다.
TCP readiness 이후 별도로 서명 put/get·SHA-256 대조, 익명/오서명/범위 밖 작업 및
client의 버킷 생성 거절, 재시작 후 객체 보존을 확인한다. 이 개발용 HTTP 경로는
T9의 OpenBao/ESO·TLS 통제나 목적지 앱 업무 기동 완료를 대신하지 않는다.

## 공식 계약

- [CNPG 1.30.1 논리 복제](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/logical_replication.md)
- [PostgreSQL 18 구독](https://www.postgresql.org/docs/18/sql-createsubscription.html)
- [RKE2 요구 사항](https://docs.rke2.io/install/requirements)
- [Tailscale operator 설치](https://tailscale.com/docs/kubernetes-operator/install-operator)
- [태그 소유와 역할별 프록시](https://tailscale.com/docs/kubernetes-operator/reference/tags)
- [명시적 egress ProxyGroup](https://tailscale.com/docs/kubernetes-operator/egress/access-tailnet-service)
- [PgBouncer 서버 TLS 검증](https://www.pgbouncer.org/config)
- [SeaweedFS 4.52](https://github.com/seaweedfs/seaweedfs/releases/tag/4.52)
- [고정 버전 S3 작업 권한](https://github.com/seaweedfs/seaweedfs/blob/984eac6a790be89e2a11de175a7413e1e3e32dd3/weed/s3api/s3api_server.go)

T2/T6/T8/T9/T30 전체 완료나 실제 데모 머신의 준비 완료는 이 문서만으로 판정하지 않는다.
