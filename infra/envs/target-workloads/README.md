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
실제 경로의 직접·중계 여부, MTU, ACL 허용·거부는 [읽기 전용 점검 도구](../../../docs/operations/t30-tailnet-path-check.md)로 확인한다.

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

## 데모용 노출 상태: 열린 버킷에서 시작해 닫는다 (T9 #13)

데모 10장 0:00 장면은 "게이트 1차에서 열린 첨부 저장소로 BLOCK"이다. 통제 IAM-01.1(인증 없이 첨부
객체를 읽을 수 있는가)이 목적지 SeaweedFS에 익명 `GET`을 보내 403이어야 PASS다. `object_exposure.py`는
`s3.json`의 두 상태를 렌더하고, 실제 상태를 익명 GET으로 관측하며, 운영자 순서를 출력한다. 클러스터에는
닿지 않는다.

| 상태 | identity | 용도 |
|---|---|---|
| `closed` | bootstrap(Admin/Read/Write/List/Tagging) · client(Read/Write/List/Tagging), 정확한 버킷만 | 위 "익명 identity 없음" 원칙 그대로. 컷오버 전 반드시 이 상태 |
| `open-demo` | `closed` + `anonymous`에 **`Read:<버킷>` 하나만** | 데모 시작 상태. `--acknowledge-anonymous-read`와 `--demo-run-id` 없이는 렌더를 거부한다. Write · List · Tagging · Admin · 다른 버킷은 열지 않는다 |

```sh
export BRAVO_OBJECT_BOOTSTRAP_ACCESS_KEY=… BRAVO_OBJECT_BOOTSTRAP_SECRET_KEY=…   # 메모리에서만
export BRAVO_OBJECT_CLIENT_ACCESS_KEY=…    BRAVO_OBJECT_CLIENT_SECRET_KEY=…
python3 object_exposure.py plan --state open-demo            # 순서만 출력
python3 object_exposure.py render --state open-demo --output /dev/shm/s3.json \
  --acknowledge-anonymous-read --demo-run-id demo-2026-11-07  # 파일 0600, stdout에는 키 없음
# Secret 교체 → rollout restart → 관측 → 게이트
python3 object_exposure.py probe --endpoint http://seaweedfs.target-dev.svc.cluster.local:8333 \
  --bucket demo-chat-attachments --key <기존 객체 키>          # state_observed=open 기대
python3 object_exposure.py render --state closed --output /dev/shm/s3.closed.json   # 닫기
```

`probe`는 응답 코드만 분류한다(`ALLOWED`/`DENIED`/`UNKNOWN:<사유>`), 객체 내용은 보관하지 않는다.
Secret을 바꾼 뒤 SeaweedFS가 설정을 다시 읽는지는 아직 실측하지 않았으므로 순서에 `rollout restart`를
넣었다. 열린 상태의 증빙은 게이트 readback(IAM-01.1 BLOCK)이고, 이 스크립트의 관측은 리셋 확인용이다.
시험: `scripts/tests/test_object_exposure.py`(렌더 상태 · 범위 · 승인 플래그 · 키 비노출 · 탐침 분류).
실제 클러스터에서 열고 닫은 실측은 아직 없다.

### 리허설 전용 시험 버킷 (T15 버킷 정책 작업)

`render --rehearsal-bucket rehearsal-attachments`를 주면 `rehearsal-object-admin` identity가 추가된다.
이 identity는 **그 버킷에만** Admin · Read · Write · List · Tagging를 갖고 운영 `demo-chat-attachments`에는
어떤 action도 없다. 키는 `BRAVO_OBJECT_REHEARSAL_ACCESS_KEY` · `_SECRET_KEY`로 받는다. T15 검증 Job이 이 identity로
버킷을 만들고 번역된 정책을 적용 · 관측한다. 운영 버킷과 분리된 상태를 시험으로 고정했다
(`scripts/tests/test_rehearsal_resources.py`). 리허설 namespace에서 8333으로 닿는 NetworkPolicy는 T30 질문 6번이다.

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

## Pre-cutover pooler and attachment copy

`pooler.py` consumes the verified `source-for-pooler` egress Service IPv4 address
and UID. It preserves the original source certificate hostname in `hostAliases`
and `server_tls_sslmode=verify-full`. Client TLS uses the target CNPG certificate
with the predeclared target-pgbouncer SAN. The writable config init container,
one replica/Recreate strategy, admin identity and login retry follow PR #114
commit `65797a1bcc60f71b51d2f7c2d5598914f95790ea`. Runtime pooler authentication
is a new target Secret copied in memory from the source pooler, because the
backend is still source. The independent target application credential is not
changed. Future host-only retargeting is insufficient: destination credential
rotation, DB role ownership, CA and hostname routing require their own reviewed
cutover contract.

Use the same reviewed cutover-access renderer/verifier with `pooler_only=true`
and `check_app=false` until a destination app exists. Bind the grant to the exact
live pooler Pod and recheck UID/owners around verification. Pooler access does
not establish app startup, business readiness or cutover readiness.

`sync_attachments.py` accepts read-only source and conditional-create destination
adapters. Compare the source bucket inventory to the database attachment keys,
reject unrelated destination objects, preserve conflicting existing bytes and
verify every copied byte with readback. The current development bound is 100
objects and 16MiB total; larger inputs require an explicitly reviewed transfer.
The operator transport uses memory/stdin and emits only bounded counts/content
hashes; object keys, filenames, bytes and credentials are not public evidence.
Copy only while source attachment writes are quiescent. Remove only the exact
synthetic development canary after its known content hash is verified; never
clear a bucket to make counts match. This is a one-way attachment copy, not
continuous object replication or the complete T9 security configuration.

A prior source readiness canary may exist beside DB-referenced attachments.
The default still rejects every unexplained source object. A trusted operator
may supply `approved_extra_keys` only after matching each extra key to retained
creation/use evidence and freezing its size and content hash. The result reports
DB-referenced and approved-extra counts separately. This is an explicit exact
inventory, not a prefix wildcard or a reason to delete either side's objects.

## 첨부 복사 실행 (컷오버 직전 1회)

`attachment_copy.py`는 DB가 가리키는 첨부 객체를 출발지 S3에서 목적지 SeaweedFS로 한 번
복사한다. 출발지에는 쓰지 않고 목적지의 기존 객체를 덮어쓰지 않으며, 다시 돌려도 안전하다.
실제 S3 · 실제 SeaweedFS 배포 · Tailscale 경로에서는 아직 재지 않았다
([증거](../../../docs/evidence/t8-attachment-sync-2026-11-04/README.md)).

| 파일 | 역할 |
| --- | --- |
| `sync_attachments.py` | 목록 대조 · 복사 · 대조 판정(`synchronize`) |
| `attachment_stores.py` | S3 · SeaweedFS 어댑터. 출발지는 쓰기 메서드가 없고 목적지 `create`는 `If-None-Match: *`로 기존 키를 거부한다 |
| `attachment_copy.py` | DB 목록 읽기, 총량 사전 검사, 영수증, 실행 명령, T12용 함수 |

### 실행 전 조건

- 출발지 첨부 쓰기가 멈춰 있어야 한다. 앱의 `PUT /api/attachments`는 쓰기 차단 중에만 막히고,
  복사가 끝난 뒤 들어온 업로드는 이 명령이 알 수 없다. 그 행은 복제되지만 객체는 목적지에 없다.
  데모 중 업로드는 앱에서 막기로 정했다(T22 · T28, 아직 반영 전).
- 목적지 버킷이 있어야 한다. 이 명령은 버킷을 만들지 않는다.
- 현재 한도는 객체 100개, 합계 16MiB다. 넘으면 아무것도 쓰지 않고 거부한다.

### 접속 정보

값은 환경 변수로만 받고 출력하지 않는다. Pod에서는 `secretKeyRef`로 넣는다.

| 변수 | 내용 |
| --- | --- |
| `ATTACHMENT_SOURCE_BUCKET`, `ATTACHMENT_SOURCE_REGION` | 출발지 버킷과 리전 |
| `ATTACHMENT_SOURCE_BUCKET_OWNER` | 버킷 소유 계정 ID. 실제 S3에서 필수 |
| `ATTACHMENT_SOURCE_ENDPOINT_URL` | 로컬 시험 전용. 실제 S3에서는 비운다 |
| `ATTACHMENT_TARGET_ENDPOINT_URL`, `ATTACHMENT_TARGET_BUCKET` | SeaweedFS S3 주소와 버킷 |
| `ATTACHMENT_TARGET_ACCESS_KEY_ID`, `ATTACHMENT_TARGET_SECRET_ACCESS_KEY` | `target-object-client` Secret의 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` 값 |
| `ATTACHMENT_APPROVED_EXTRA_KEYS_FILE` | (선택) 개별 확인한 추가 객체 키를 한 줄에 하나씩 적은 파일. 저장소에 넣지 않는다 |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLROOTCERT` | 출발지 DB. `attachments`를 SELECT할 수 있는 기존 역할을 쓴다 |
| `PGSSLMODE` | `verify-full`. 다른 값이면 연결하기 전에 거부한다 |
| `ATTACHMENT_DB_ALLOW_LOOPBACK_PLAINTEXT` | 로컬 시험 전용. 값이 `1`이고 DB의 `host`와 `hostaddr`가 모두 `127.0.0.1` 또는 `::1`일 때만 `verify-full`을 요구하지 않는다. 실제 실행과 터널에서는 넣지 않는다 |

출발지 S3 자격은 SDK 기본 경로(역할, 운영자 프로필)에서 온다. 목적지 키는 AWS 키가 아니므로 섞지 않는다.

DB 접속 판단은 비밀번호가 나가기 전에 끝난다. libpq의 우선순위대로 DSN, 그다음 `PG*`를 읽어 판단하고, `verify-full`을
연결 인수로 고정한다. 판단을 뒤집을 수 있는 설정은 연결하기 전에 거부한다. 서비스 파일(`PGSERVICE`, DSN의 `service`)은
`PG*`보다 우선해 다른 호스트를 가리킬 수 있고, Unix 소켓은 libpq가 `sslmode`와 상관없이 TLS를 쓰지 않으며, 호스트 목록에는 소켓이 섞일 수 있다.

### 명령

`uv`가 고정된 boto3 · psycopg로 실행한다. 표준 출력에 JSON 한 줄만 나온다. 성공은 종료 코드 0, 실패는 1이다.

```sh
infra/envs/target-workloads/attachment_copy.py
```

SeaweedFS는 `bravo-object-client=true` Pod만 받는다. 운영자 머신에서 돌릴 때는
`kubectl port-forward`로 연결하고 `ATTACHMENT_TARGET_ENDPOINT_URL`을 그 로컬 주소로 둔다.
DB를 터널로 잇는다면 `PGHOST`에 인증서 이름, `PGHOSTADDR`에 로컬 주소를 넣어 `verify-full`을 유지한다.
이 터널 경로는 아직 재지 않았다.

### 영수증과 실패

성공 영수증은 건수(`copied`, `reused`, `source_count`, `destination_count`,
`database_referenced_count`, `operator_approved_extra_count`, `bytes_verified`)와
해시(`key_content_inventory_sha256`, `content_sha256`)만 담는다. 실패 영수증은 `stage`,
`reason`, `error_category`, `s3_error_code`만 담는다. 허용 목록에 없는 메시지와 오류 코드는
`unclassified`로 바뀐다. 객체 키 · 본문 · 주소 · 자격이 메시지에 섞여 나가지 않게 하기 위해서다.

| `reason` | 뜻 | 다음 행동 |
| --- | --- | --- |
| `source bucket and database attachment inventory differ` | 출발지 객체와 DB 참조가 다르다 | 원인을 찾는다. 설명된 객체만 추가 목록에 넣고, 맞추려고 지우지 않는다 |
| `destination contains unrelated objects; preserve and reconcile` | 목적지에 무관한 객체가 있다 | 사람이 확인한다. 비우지 않는다 |
| `destination conflict; existing object is preserved` | 같은 키에 다른 내용이 있다 | 사람이 확인한다. 자동으로 덮어쓰지 않는다 |
| `source changed during transfer; require quiescent retry`, `final object inventory changed` | 복사 중 출발지가 바뀌었다 | 쓰기를 멈추고 다시 돌린다. 이미 옛 내용이 복사됐다면 재실행은 위의 충돌로 멈추므로 사람이 정리한다 |
| `bounded transfer size exceeded`, `bounded binary source object required`, `bounded nonempty attachment inventory required` | 한도 초과이거나 DB 목록이 비었다 | 한도를 검토한다 |
| `database connection must use verify-full TLS` | DB를 검증 없이 연결하려 했다. 연결하기 전에 멈췄다 | `PGSSLMODE`, `PGSSLROOTCERT`를 고친다 |
| `database target must be one host from the DSN or PG* environment` | 서비스 파일, Unix 소켓, 호스트 목록을 썼거나 호스트가 없다. 연결하기 전에 멈췄다 | `PGHOST`나 DSN의 `host`에 호스트 하나를 적는다 |
| `unclassified` + `s3_error_code` 또는 `error_category` | 네트워크 · 인증 · 설정 문제 | 원인을 고치고 다시 돌린다. 끝난 객체는 `reused`로 넘어간다 |

### T12에서 호출

함수는 `run_copy`다. 실패하면 안전한 필드만 가진 `AttachmentCopyFailed`를 던지고, 원래 예외는 메시지에 넣지 않는다.

```python
from attachment_copy import AttachmentCopyFailed, run_copy

try:
    # db_dsn을 비우면 PG* 환경 변수를 쓴다. 나머지 접속 정보는 위 표의 환경 변수에서 읽는다.
    receipt = run_copy(db_dsn=source_site.db_control_dsn)
except AttachmentCopyFailed as failure:
    ...  # 컷오버를 시작하지 않는다. failure.receipt()를 기록한다.
```

- `precheck` 앞, 쓰기 제한을 걸기 전에 부른다. 객체 수에 비례해 네트워크를 쓰므로 쓰기 정지 예산 안에 넣지 않는다.
- 복사와 쓰기 차단 사이의 업로드는 이 함수가 잡지 못한다. 쓰기 차단 뒤 DB의 고유 `object_key` 수를
  `receipt["database_referenced_count"]`와 비교하면 잡을 수 있다(T12 담당 결정).
- `source_site.db_control_dsn`의 `cutover_control`은 `attachments` SELECT 권한이 있다. 읽기 전용 트랜잭션은 이 함수가 건다.
- 지금 Executor에서 바로 부를 수는 없다. 이미지에 boto3와 이 파일들이 없고, S3 읽기 권한과
  SeaweedFS까지의 네트워크 경로도 없다. 이를 Executor에 추가할지, 운영자가 명령으로 돌리고
  영수증을 넘길지는 T12 담당이 정한다.

### 시험

기본 시험은 의존성 없이 돈다. 실제 객체 시험은 Docker와 SDK가 필요하며 `t8-attach-` 컨테이너를 띄우고 지운다.

```sh
T8_ATTACH_E2E=1 uv run --no-project --python 3.12 --with boto3==1.43.123 --with 'psycopg[binary]==3.3.6' \
  python -m unittest discover -s scripts/tests -p 'test_attachment_copy*.py' -v
```

`scripts/tests/test_attachment_copy_wire.py`는 비밀번호를 요구하는 가짜 PG 서버로 실제 libpq가 보내는 바이트를 확인한다.
psycopg만 있으면 되고 컨테이너는 필요 없으며, 위 명령에도 포함된다.

`scripts/tests/attachment_mutation_check.py`는 임시 복사본에서 검사를 하나씩 빼고 시험이 실패하는지 확인한다.

## 정방향 구독 재개 (재발 대비)

`t8_source_to_target`가 연결 끊김으로 스스로 꺼졌을 때 쓰는 명령이다. 기존 슬롯을 그대로 두고 그 구독만 다시 켠 뒤,
실제로 따라잡았는지 확인한다. 슬롯 · 역할 · 역방향 구독 · 앱의 쓰기 차단은 건드리지 않고, 목적지에는 어떤 쓰기도 하지 않는다.

첫 실제 재개(11/4 16:26 KST)는 이 명령 없이 수행됐고 결과의 정본은 [#201](https://github.com/Softbank-Hackathon-Finals/platform/pull/201)이다.
이 명령은 재발에 대비한 절차로, 로컬 컨테이너에서만 재현했고 실제 환경에서는 돌리지 않았다.
출력은 #201과 같은 이름과 단위를 쓴다. 측정과 설정 판단은 [증거 문서](../../../docs/evidence/t8-replication-resume-2026-11-04/README.md)에 있다.

### 절차

값은 환경 변수로만 받고 출력하지 않는다. 표준 출력에 JSON 한 줄만 나오고, 성공은 종료 코드 0, 그 밖은 전부 1이다.

1. 앱 Pod 안에서 `GET /admin/write-block`으로 출발지 앱의 쓰기 차단 상태를 읽는다. 이 명령은 DB만 보므로 이 값을 읽지도 바꾸지도 않는다.
   재개는 어느 상태에서나 할 수 있으며, 읽은 값을 `--source-write-block on|off`로 넘기면 출력에 남는다.
   차단은 LOGIN과 별개다. LOGIN은 명령이 직접 읽어 `source_app_cannot_login`으로 판단한다.
2. 옵션 없이 돌린다. 어떤 쓰기도 하지 않는다. `refusals`가 비어 있어야 한다.
3. 출력의 `preflight.source.system_identifier`와 `preflight.target.system_identifier`가 의도한 두 클러스터인지
   대조한다. 명령은 어느 쪽이 맞는지 알 수 없으므로 이 대조는 사람이 한다.
4. 대조한 값을 넘겨 `--apply`로 돌린다.

```sh
infra/envs/target-workloads/resume_replication.py --source-write-block on
infra/envs/target-workloads/resume_replication.py --apply --source-write-block on --source-system-id <값> --target-system-id <값>
```

| 구독 상태 | 옵션 없음 | `--apply` |
| --- | --- | --- |
| 꺼짐 | 사전 확인만. `would_run`에 실행할 문장을 보여 준다 | 사전 확인 → ENABLE 1회 → 기준점까지 따라잡기 → 시험 행 왕복 → 유지 확인 |
| 이미 켜짐 | 사전 확인 → 따라잡기 → 유지 확인. 쓰기 없음 | ENABLE 없이 따라잡기 → 시험 행 왕복 → 유지 확인 |

시험 행 정리가 `pending`으로 끝났다면 `--cleanup-marker <barrier_id>`로 마저 끝낸다. 아래 시험 행 절을 본다.

이미 다른 사람이 켠 구독에 돌려도 다시 켜지 않는다(`continuity.enable_submissions`가 0). 쓰기를 전혀 원하지 않으면 옵션 없이 돌린다.

### 접속 정보

| 변수 | 쓰는 곳 | 필요한 권한 |
| --- | --- | --- |
| `T8_RESUME_SOURCE_DSN` | 출발지 읽기 | `chat_replicator`로 충분하다. 슬롯과 업무 테이블, `cutover.barrier`를 읽는다 |
| `T8_RESUME_TARGET_DSN` | 목적지 읽기와 ENABLE | 읽기는 `chat_replicator`로 충분하다. ENABLE은 `cutover.set_subscription` 실행 권한 또는 구독 소유 권한이 필요하다 |
| `T8_RESUME_EXECUTOR_DSN` | 실행 기록 읽기 | `executor.executions`, `leases`, `resource_fences` SELECT |
| `T8_RESUME_SOURCE_PROBE_DSN` | `--apply`와 `--cleanup-marker`의 시험 행 쓰기 | `cutover.barrier` INSERT · SELECT · DELETE. `T8_RESUME_SOURCE_DSN`과 같은 클러스터의 같은 DB여야 한다 |
| `T8_RESUME_ALLOW_LOOPBACK_PLAINTEXT` | 로컬 시험 전용 | 값이 `1`이고 host와 hostaddr가 모두 loopback 주소일 때만 `verify-full`을 요구하지 않는다 |

- 모든 DSN은 `verify-full`이어야 한다. 비밀번호가 나가기 전에 판단한다. 규칙은 첨부 복사와 같다.
- `verify-full`은 어느 서버가 답했는지만 보장한다. 쓰기 DSN이 검증한 출발지인지는 따로 본다. 그 연결에서 `system_identifier`와 DB 이름을 읽어 출발지와 대조하고, 다르거나 읽지 못하면 ENABLE과 INSERT 전에 거부한다. 쓰는 세션에서도 쓰기 직전에 다시 대조한다.
- `cutover_control`은 `cutover.barrier`에 DELETE가 없어 시험 행 쓰기에 쓸 수 없다. 그 역할을 넓히지 않는다.
  그런 역할이 없으면 `--no-barrier-probe`로 건너뛰고 출력에 그 사실을 남긴다.
- `target_origin_remote_lsn`은 `pg_read_all_stats`로도 읽을 수 없다(실측: `cutover_control`). 읽지 못하는 역할이면 값은 `null`이고,
  시험 행 삭제가 도착한 것으로 적용을 확인한다(`continuity.applied_position`).
- Executor가 없는 환경이면 `--executor-not-deployed`로 그 사실을 적는다. 출력에 남고, `cutover_control` 세션 검사는 계속된다.
  이 진술로 실행 중인 Executor를 숨길 수 없도록, `T8_RESUME_EXECUTOR_DSN`이 있으면 그쪽 값을 따른다.

ENABLE 권한이 없어 `no_enable_privilege`만 남았다면 권한을 넓히지 말고, 권한을 가진 사람이 `would_run`의 문장을
실행한 뒤 옵션 없이 다시 돌려 확인한다. 이미 켜진 구독은 ENABLE 권한 없이 확인할 수 있다.

### 거부 사유

`refusals`에 하나라도 있으면 아무것도 바꾸지 않는다. 확인하지 못한 상태도 안전한 상태로 치지 않는다.

| 사유 | 뜻 | 다음 행동 |
| --- | --- | --- |
| `slot_invalidated`, `slot_missing` | 슬롯에 이어 받을 WAL이 없거나 슬롯이 없다 | 재개로 고칠 수 없다. 초기 복사부터 다시 하는 절차를 사람이 정한다 |
| `slot_at_risk` | 다음 체크포인트에 WAL을 잃을 수 있다 | 사람이 바로 판단한다 |
| `slot_in_use` | 다른 프로세스가 슬롯을 쥐고 있다. 대개 아직 끊기지 않은 이전 walsender다 | `wal_sender_timeout`이 지난 뒤 다시 돌린다. 켜도 5초마다 재시도만 한다. 그 뒤에도 남아 있으면 누가 읽는지 찾는다 |
| `reverse_active`, `reverse_slot_in_use` | 역방향이 돌고 있다 | 쓰기 방향이 정리될 때까지 멈춘다 |
| `target_app_can_login`, `target_app_sessions` | 목적지 앱이 쓸 수 있거나 세션이 남아 있다 | 쓰기 DB가 둘일 수 있다. 원인을 먼저 찾는다 |
| `source_app_cannot_login` | 출발지 앱 역할이 NOLOGIN이다 | 컷오버 중이거나 실패한 뒤다. 실행 기록을 본다 |
| `executor_running`, `control_session_present` | 실행이 lease를 쥐고 있거나 제어 세션이 있다 | 끝나거나 사람이 풀 때까지 기다린다 |
| `executor_state_unconfirmed` | 실행 기록을 읽지 못했다 | 접속을 고치거나, 없는 환경이면 `--executor-not-deployed`를 쓴다 |
| `target_ahead_of_source` | 목적지에 출발지보다 많은 행이 있다 | 우회 쓰기를 의심한다. 사람이 대조한다 |
| `source_unconfirmed`, `target_unconfirmed` | 읽지 못했다 | `errors`의 분류를 보고 접속을 고친다 |
| `source_and_target_are_one_cluster`, `*_identity_mismatch`, `*_identity_not_pinned` | 두 DSN이 같은 클러스터이거나 의도한 클러스터가 아니거나 대조값이 없다 | DSN을 고친다 |
| `no_enable_privilege` | ENABLE 권한이 없다 | 위 문단을 따른다 |
| `barrier_not_published`, `barrier_not_subscribed`, `barrier_not_readable_on_*`, `barrier_probe_not_permitted` | `--apply`의 시험 행 왕복을 할 수 없다 | 아래 시험 행 절을 본다 |
| `barrier_probe_wrong_database`, `barrier_probe_not_primary`, `barrier_probe_unconfirmed` | 쓰기 DSN이 검증한 출발지가 아니거나, 대기 서버이거나, 읽지 못했다 | `T8_RESUME_SOURCE_PROBE_DSN`을 고친다. 권한이 있어도 통과시키지 않는다 |
| `source_wrong_database` | 읽기 DSN이 슬롯이 있는 DB가 아니다 | `T8_RESUME_SOURCE_DSN`의 DB를 고친다 |
| `not_a_marker_row`, `invalid_barrier_id` | `--cleanup-marker`에 준 ID가 시험 행이 아니거나 UUID가 아니다 | 출력의 `marker.barrier_id`를 쓴다. 다른 실행의 행은 지우지 않는다 |

### 출력과 #201의 대응

같은 이름은 같은 뜻과 단위다. #201에 있으나 여기에 없는 값은 이 명령이 재지 못하거나 코드상 상수인 것이다
(`slot_recreations`, `publication_refreshes`, `legacy_fences`, 비공개 영수증 해시).

| 자리 | 항목 |
| --- | --- |
| `immediately_before_enable` | `source_wal_flush_lsn`, `source_slot_confirmed_flush_lsn`, `gap_bytes`, `gap_mib`, `slot_active`, `wal_status` |
| `command_clock.first_observations` | `worker`, `received_fixed`, `applied_fixed`, `barrier`. 각각 `elapsed_s`, `previous_sample_s`, `db_utc`. 시계는 #201과 달리 이 명령을 돌리는 머신의 단조 시계다 |
| `stages.before`, `delivered`, `cleaned` | `forward_enabled`, `reverse_enabled`, `apply_workers`, `source_slot_active`, `source_slot_wal_status`, `source_slot_invalidated`, `source_sender_state`, `source_sender_tls`, `source_slot_confirmed_flush_lsn`, `target_origin_remote_lsn`, `target_app_login`, `target_apply_errors`, `target_sync_errors`, `business_tables` |
| `marker` | `insert_lsn_before_commit`, `delete_lsn_before_commit`, `same_row_on_both_sides_when_present`, `source_insert_commits`, `source_delete_commits`. 이 명령은 여기에 `source_insert_submissions`, `source_delete_submissions`, `cleanup`을 더한다 |
| `continuity` | `same_subscription_oid_and_origin`, `enable_submissions`, `table_sync_states`, `source_wal_sender_timeout_ms`, `target_wal_receiver_timeout_ms`, `disable_on_error`, `platform_before_after` |

`business_tables`의 해시는 첫 출발지 조회 시점까지의 행만 대상으로 한다. 출발지에 쓰기가 계속돼도 비교할 수 있게 하려는 것이며,
해시 값 자체는 출력하지 않는다. 시험 행 없이 돌린 실행의 마지막 관측은 `stages.final`에 남는다.

### 사후 확인

`catch_up`이 먼저 통과해야 시험 행을 넣는다. `post_check`는 그 뒤에 모든 항목이 `--settle`(기본 15초) 동안 유지돼야 통과한다.
각각 `--timeout`(기본 900초) 안에 확인하지 못하면 실패다.

| 항목 | 확인하는 것 |
| --- | --- |
| `forward_enabled`, `apply_worker_running` | 구독이 켜져 있고 적용 작업자가 있다 |
| `source_slot_active` | 작업자가 실제로 출발지에 붙어 슬롯을 쥐었다. 작업자는 접속 전에도 보이므로 이 항목이 따로 필요하다 |
| `source_slot_valid` | 따라잡는 동안 슬롯이 무효가 되지 않았다 |
| `confirmed_flush_advanced` | 슬롯의 확인 위치가 재개 전보다 앞으로 갔다 |
| `received_fixed` | 목적지 워커의 수신 위치가 재개 직전 출발지 flush 위치(`positions.fixed`) 이상이다 |
| `confirmed_fixed` | 슬롯의 `confirmed_flush_lsn`과 워커의 `latest_end_lsn`이 모두 그 위치를 지났다 |
| `applied_fixed` | origin 적용 위치가 시험 행 삭제 커밋 전 위치를 지났다. 시험 행을 보내고 읽을 수 있을 때만 본다. origin은 커밋이 있어야 움직여서 WAL만으로는 판단할 수 없다 |
| `tables_ready`, `rows_arrived` | 복사 중인 테이블이 없고, 첫 조회 시점의 행이 목적지에 모두 있다 |
| `errors_not_increased` | 적용 오류 · 동기화 오류 · 충돌 수가 재개 전보다 늘지 않았다 |
| `single_writer_kept` | 출발지 앱 LOGIN, 목적지 앱 NOLOGIN, 역방향 OFF가 그대로다 |

구독이 다시 꺼지거나 오류가 늘면 기다리지 않고 바로 실패로 끝난다. 실패해도 구독을 끄거나 되돌리지 않는다.
원인이 연결이었다면 연결이 돌아온 뒤 같은 명령을 다시 돌린다. 충돌이라면 다시 돌려도 또 꺼지므로 원인부터 찾는다.
끝날 때 해시가 다르거나, 그 사이 실행이 lease를 쥐었거나, 구독 OID가 바뀌었어도 실패다.

### 시험 행

`--apply`는 따라잡은 뒤 `cutover.barrier`에 `epoch` 0인 행 하나를 넣고, 목적지 도착을 기다렸다가 지우고, 삭제 도착까지 기다린다.
INSERT는 한 번만 보낸다. 지우는 것은 이 실행의 UUID이면서 `epoch`가 0인 행뿐이고, 목적지 행은 지우지 않고 보고만 한다. 다른 실행의 행에는 손대지 않는다.

제출과 확인을 나눠 적는다. `*_submissions`는 보낸 횟수이고, `*_commits`는 응답이나 출발지 재조회로 확인한 결과다. 커밋한 뒤 응답만 끊기면 재조회로 찾아 지운다. `source_insert_commits`가 `null`이면 커밋 여부를 모른다는 뜻이다. 응답 유실 직후 행이 보이지 않는 것만으로 0으로 바꾸지 않는다.

`marker.cleanup`은 다음 둘 중 하나다.

| 값 | 뜻 | 다음 행동 |
| --- | --- | --- |
| `confirmed` | 같은 UUID의 INSERT가 끝난 뒤 삭제를 커밋하고, 목적지 삭제 반영과 양쪽 행 부재를 확인했다 | 없음. 왕복이 실패했다면 원인을 고쳐 `--apply`를 다시 돌린다. 새 UUID로 보낸다 |
| `pending` | 없음을 확인하지 못했다. `pending_on`이 어느 쪽인지 알려 준다 | `marker.resume`의 명령을 대조값을 채워 돌린다 |

```sh
infra/envs/target-workloads/resume_replication.py --cleanup-marker <barrier_id> --marker-protocol serialized-v1 --source-system-id <값> --target-system-id <값>
```

`--cleanup-marker`는 ENABLE이나 INSERT를 하지 않는다. 행이 보이지 않아도 같은 UUID의 트랜잭션 잠금을 얻은 뒤 DELETE를 커밋하므로, 앞선 INSERT의 늦은 COMMIT을 지나쳐 정리 완료로 보고하지 않는다. 여러 번 돌려도 된다. `pending_on`이 `target`뿐이면 출발지 삭제는 끝난 것이다. 복제가 멈춰 있을 수 있으니 구독 상태를 먼저 본다. 목적지 행은 이 명령도 지우지 않는다.

새 실행은 `marker.protocol=serialized-v1`을 기록한다. INSERT는 UUID에서 계산한 트랜잭션 advisory lock을 얻었다는 응답을 받은 뒤에만 제출하며, DELETE도 같은 잠금을 얻는다. 잠금 대기가 제한 시간을 넘으면 `cleanup=pending`으로 남는다. 목적지 INSERT를 관측하지 못한 실행은 행 부재에 더해 origin 적용 위치가 DELETE 커밋 응답 뒤 읽은 WAL 위치 이상인지 확인한다. origin을 읽을 권한이 없거나 적용이 멈췄으면 성공으로 바꾸지 않는다.

UUID는 잠금 계산과 SQL 바인딩 전에 소문자·하이픈 포함 표준 문자열로 정규화한다. 정리 명령도 같은 표현을 기록하므로 대문자나 하이픈 생략 표기가 원래 INSERT의 잠금을 우회하지 못한다. 기존 표준 UUID의 `serialized-v1` 잠금 키는 바뀌지 않는다.

정리 명령의 `--marker-protocol`은 원래 실행 기록에서 복사한다. 이 필드가 없는 이전 도구의 기록에는 잠금 보장이 없으므로 새 명령이 거부한다. 이전 실행에 이 값을 임의로 추가하지 말고, 원래 서버 세션의 종료와 커밋 상태를 운영자가 확인한 뒤 별도로 정리한다.

읽기·쓰기 연결의 `search_path`는 `pg_catalog,pg_temp`로 고정하고, DB 이름과 클러스터 신원 함수는 `pg_catalog`를 명시한다. 따라서 `public.current_database()` 같은 함수나 DB 기본 `search_path`가 등록된 DB 신원을 대신할 수 없다.

`cutover.barrier`는 기본 `schema.sql`의 publication에 없다. 환경마다 추가하고 구독을 `copy_data=false`로 갱신하는 단계다
([T12 계약](../../../docs/contracts/T12-platform-integration.md)). 그래서 재개의 조건으로 삼지 않고 사전 확인에서 읽는다.

- 옵션 없는 실행은 빠진 단계를 `barrier_gaps`로 알려 주고 통과할 수 있다. 업무 테이블 복제는 barrier 없이도 돈다.
- `--apply`는 빠져 있으면 거부한다. 왕복을 건너뛰려면 `--no-barrier-probe`를 쓴다.
- `barrier_gaps`가 남아 있으면 Executor의 5단계가 barrier를 기다리다 실패한다. 재개와 별개로 컷오버 전에 맞춘다.

### 하지 못하는 것

- 끊김 자체를 막지 않고 설정도 바꾸지 않는다. `disable_on_error`가 켜져 있는 동안 연결이 다시 끊기면 같은 이유로 또 꺼진다. 설정 판단(정방향은 끄는 방안 검토, 역방향은 검증 후 결정)은 증거 문서에 있다.
- 사전 확인과 ENABLE 사이의 짧은 틈은 막지 못한다. 그 사이에 컷오버가 시작되면 Executor의 사전 점검이 구독 상태를 다시 본다.
- 이 명령의 세션도 `system_roles` 밖의 역할이면 Executor가 우회 쓰기 후보로 본다. 컷오버 중에는 돌리지 않는다.

### 시험

아래 첫 명령은 의존성 없이 돈다. 이어지는 네 명령은 `t8-lag-` 컨테이너를 띄우고 지운다.

```sh
python3 -m unittest discover -s scripts/tests -p 'test_resume_replication.py'
infra/envs/target-workloads/testing/replication_resume_lab.py recover --out recover.json
infra/envs/target-workloads/testing/replication_resume_lab.py refusals --out refusals.json
infra/envs/target-workloads/testing/replication_resume_lab.py paths --out paths.json
infra/envs/target-workloads/testing/replication_resume_lab.py timeouts --out timeouts.json
```

두 결함의 TLS/실제 PostgreSQL 회귀 시험은 별도로 실행한다. Docker와 OpenSSL이 필요하며, 임의 비밀번호와 localhost 포트의 일회용 DB만 사용한다. 시험은 소유 라벨로 컨테이너·네트워크를 확인하고 정리한다.

```sh
T8_RESUME_WIRE=1 uv run --with pytest --with 'psycopg[binary]==3.3.6' \
  pytest infra/envs/target-workloads/testing/test_resume_replication_wire.py -q
```

2026-11-05 수정 및 앱 TLS 시험 범위는 [후속 검증 기록](../../../docs/evidence/t8-resume-fixes-2026-11-05/README.md)에 있다.
