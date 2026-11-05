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

앱 소스는 ce5727e부터 확인한 게시본 8d82a92까지 시작 시 AWS Secrets Manager SDK를 필수로 호출한다.
OpenBao/ESO의 환경 변수만으로 같은 이미지가 AWS 없이 시작한다고 주장하지 않는다.
OCI 원본 바이트 수입/패키징 확인과 실제 목적지 업무 기동을 구별하며, 앱 의존성 변경은
T11/T15의 검증·승인 경계에 따라 처리한다. 개인키·AWS 자격을 이미지에 넣지 않는다.
개발 측정용 앱 배포는 아래 [개발 측정용 앱](#개발-측정용-앱)을 따른다.

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

## 개발 측정용 앱

`application.py`는 T17 · T8 · T18의 Edge 경유 측정에 쓸 `demo-chat` Deployment와 Service를 렌더한다.
**이 구성은 출구 증거가 아니다.** 앱은 여전히 Secrets Manager SDK를 부르고, 같은 Pod의 호환 컨테이너가
루프백에서 대신 응답한다. 이 Pod로 Exit Drill을 돌리면 판정은 `NOT_EXIT_EVIDENCE`로 UNKNOWN이다. 이유와 없앨 조건은 [ADR-0017](../../../docs/adr/0017-target-app-secrets-manager-compat.md)에 있다.

| 항목 | 값 | 이유 |
| --- | --- | --- |
| Service | `demo-chat.target-dev.svc.cluster.local:8080` (ClusterIP) | Tunnel이 가리킬 주소. 다른 공개 경로는 만들지 않는다 |
| `APP_ENV` | `onprem` | `X-Bravo-Env` 값. T18 `--target-env`, T37 `BRAVO_ENV`와 같다 |
| `DB_HOST` | `target-pgbouncer` | 컷오버 전에는 출발지 DB로 간다. 목적지 DB에 직접 붙지 않는다 |
| `WRITE_BLOCKED` | `true` | 항상 닫힌 채 시작한다. Pod가 다시 뜨면 다시 닫힌다 |
| `ATTACHMENT_UPLOAD_ENABLED` | `false` | 컷오버 전 업로드는 403이다. 받으면 객체는 목적지에, 행은 출발지 DB에 들어가 어긋난다 |
| probe | `/readyz`, `/healthz` | DB를 보지 않는다. DB 경로는 `/api/health/db`로 따로 본다 |

### 적용 전 조건

1. **이미지.** 쓸 이미지는 [`app-image.json`](app-image.json) 한 곳에만 적는다. 렌더러와 `scripts/export_target_image.py`가 이 파일을 읽는다.
   새로 게시하면 그 실행 아티팩트의 `image-receipt.json`(`source_sha`, `digest`)과 `registry-index.json`(amd64 `digest`)을 옮겨 적고 끝낸다.
   렌더러는 이 파일의 index digest만 받는다. 다른 digest는 저장소가 GHCR이든 ECR이든 거부한다. DB · 객체 저장소 · 관리 토큰을 받는 Pod에서 다른 바이트가 돌지 못하게 하려는 것이다. ECR에서 받을 때만 `--image`로 저장소를 바꾼다.
2. **VM에 넣기.** ECR에는 이미 있다. 이 게시본의 demo-chat 영수증(#19)과 `transfer-private-image`([실행 41815292046](https://github.com/Softbank-Hackathon-Finals/demo-chat/actions/runs/41815292046), 2026-11-04 19:36 KST)가 끝났고, 대상 바이트와 CI Pull까지 확인됐다.
   개발 VM에는 류지환 님 기록(#210)상 2026-11-04 23:05 KST에 묶음이 containerd에 등록됐고 23:07에 index와 amd64 manifest를 대조했다. 이 문서 쪽에서 직접 확인한 것은 아니다.
   남은 일은 이 변경이 병합된 뒤 VM 담당이 digest를 대조하고 앱을 배포하는 것이다. 다른 VM이면 [이미지 인수인계](../../../docs/operations/target-image-handoff.md) 순서로 묶음을 만들어 넣고 digest 별칭을 붙인다.
   게시본을 바꾸면 앞 두 단계를 다시 한다. T22 담당이 demo-chat `main`의 `docs/evidence/image-publication-2026-11-03/` 영수증을 올리고(그전에는 `reviewed_publication_mismatch`로 멈춘다), Actions 권한자가 전달을 돌린다.

   렌더 기본값은 `imagePullPolicy: Never`다. 레지스트리에서 받으려면 `--pull-secret <이름>`을 쓰지만,
   [T7 절차](../../../docs/operations/target-registry-access.md)의 Secret은 시험 뒤 지우는 것이라 상시 자격이 없다. 쓰려면 자격 위치와 갱신 담당을 T7 담당자와 먼저 정한다.
3. **PgBouncer.** `pooler.py`의 `target-pgbouncer`가 출발지를 가리키는지 확인한다. 앱은 이 경로만 쓴다.
4. **Secret.** 아래 세 개가 있어야 한다. Secret이나 키가 없으면, 또는 `credentials.json` 형식이 틀리면 앱이 뜨지 않고 Service도 비어 있다.
   값이 틀린 경우는 막지 못한다. 토큰이 비면 앱은 뜨고 쓰기 스위치만 404다.

| Secret | 키 | 쓰임 |
| --- | --- | --- |
| `target-app-credentials` | `credentials.json` (`username`, `password`) | 컷오버 전 출발지 앱 계정. 호환 컨테이너에만 마운트한다 |
| `target-app-objects` | `access_key`, `secret_key` | SeaweedFS 서명. boto3 공용 자격이라 호환 컨테이너 호출에도 쓰이지만 AWS로 나가지 않는다 |
| `target-app-admin` | `token` | 쓰기 차단 스위치 |

T9의 `delivery`가 적용돼 있으면 이 이름으로 이미 있으므로 만들지 않는다. 먼저 확인한다.

```sh
kubectl -n target-dev get secret target-app-credentials target-app-objects target-app-admin
```

없을 때만 값을 메모리로 옮겨 만든다. 파일 · 인수 · 출력에 남기지 않고, `apply` 대신 `create`를 쓴다.
목적지 역할의 `target-app-db`가 아니라 출발지 비밀번호를 쓴다. 풀러 뒤가 아직 출발지이기 때문이다.
T9를 나중에 적용할 때 ESO가 이 Secret을 넘겨받는지는 재지 않았다. 그때 T9 담당자가 확인한다.

```sh
set -o pipefail  # 앞 명령이 실패했는데 빈 값으로 Secret이 만들어지는 것을 막는다.
kubectl -n target-dev get secret target-pooler-source-app -o json | python3 -c '
import base64, json, sys
password = base64.b64decode(json.load(sys.stdin)["data"]["password"]).decode()
json.dump({"username": "chat_app", "password": password}, sys.stdout)' \
  | kubectl -n target-dev create secret generic target-app-credentials --from-file=credentials.json=/dev/stdin
kubectl -n target-dev get secret target-object-client -o json | python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]
json.dump({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "target-app-objects", "namespace": "target-dev"},
           "data": {"access_key": data["AWS_ACCESS_KEY_ID"], "secret_key": data["AWS_SECRET_ACCESS_KEY"]}}, sys.stdout)' \
  | kubectl create -f -
python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))' \
  | kubectl -n target-dev create secret generic target-app-admin --from-file=token=/dev/stdin
kubectl -n target-dev label secret target-app-credentials target-app-objects target-app-admin component=target
```

### 적용과 확인

```sh
python3 application.py > "$OUTPUT_DIR/application.json"
kubectl apply --server-side --dry-run=server -f "$OUTPUT_DIR/application.json"
kubectl apply --server-side -f "$OUTPUT_DIR/application.json"
kubectl -n target-dev rollout status deployment/demo-chat
```

한 터미널에서 포트를 열고(전경에서 멈춰 있는다), 다른 터미널에서 확인한다.

```sh
kubectl -n target-dev port-forward service/demo-chat 18080:8080
```

```sh
curl -si http://127.0.0.1:18080/api/info        # X-Bravo-Env: onprem, write_blocked: true
curl -si http://127.0.0.1:18080/api/health/db   # 200이어야 DB 경로가 열린 것이다
```

**`/api/health/db`가 200이 아니면 Edge에 넣지 않는다.** `/readyz`는 DB를 보지 않아서, 아래처럼 DB가 막혀도 Pod는
Ready이고 Service에 잡힌다. Edge 헬스 체크가 `/readyz`나 `/healthz`면 이 상태를 정상으로 본다.

| 상태 | Pod · Service | `/readyz` | `/api/health/db` | `GET /api/messages` | `POST /api/messages` |
| --- | --- | --- | --- | --- | --- |
| 목적지 `chat_app` NOLOGIN (직접, 풀러 경유 모두) | Ready, 엔드포인트 1 | 200 | 503 (2초) | 503 + `Retry-After: 2` (5초) | 차단 중 503 즉시, 해제해도 503 (5초). 행 없음 |
| 풀러 → 출발지 | Ready, 엔드포인트 1 | 200 | 200 | 200 | 차단 중 503, 해제 뒤 201. 행은 출발지에만 |
| `credentials.json` 없음 · 형식 오류 · Secrets Manager 응답 없음 | Ready 아님, 엔드포인트 0 | 요청이 앱까지 가지 않는다 | | | |

시험에서 판정한 앱 응답에는 모두 `X-Bravo-Env: onprem` 하나만 붙었다. 측정값과 대역은
[증거](../../../docs/evidence/t30-target-app-2026-11-04/README.md)에 있다.

**풀러가 시작된 뒤 첫 로그인은 맞는 비밀번호여야 한다.** PgBouncer 1.25.1은 프로세스가 뜬 뒤 첫 시도가 아니면
SCRAM 키를 저장하지 않는다. 첫 시도가 틀리면 그 뒤에 맞는 비밀번호로 와도 서버 로그인이 실패하고 저절로 낫지 않는다.
`/api/health/db`가 계속 503이고 풀러 로그에 `did not provide SCRAM keys`가 보이면 이 상태다. 앱 Secret을 고친 뒤
`kubectl -n target-dev rollout restart deployment/target-pgbouncer`로 회복한다(잰 방법). 풀러를 재시작하기 전에는 옛 비밀번호를
가진 클라이언트(이전 앱 Pod 등)를 먼저 멈춘다. 첫 로그인이 성공한 뒤의 틀린 시도는 영향이 없다. `auth_file`이 바뀐 뒤의
`RELOAD`도 소스상 이 상태를 풀지만 재지 않았다.

### 쓰기 스위치

목적지 앱 쓰기를 열어도 행은 풀러 뒤의 DB 하나에만 들어간다. 다만 `/api/health/db`와 풀러 대상을 확인하기 전에는
열지 않는다. 측정 담당자가 정해 여는 것이고 자동으로 열지 않는다. 롤아웃이 끝난 뒤에 실행한다.
재시작하면 다시 닫히므로 측정 중에는 앱을 재시작하지 않는다. 마지막 인수가 `false`면 열고 `true`면 닫는다.
앱에는 종료 전 5초 대기(preStop)가 있다. 없으면 재시작 직후 요청 12개 중 3~8개가 연결에 실패했다(k3s 10회).
그 5초 동안은 퇴장 중인 이전 Pod가 원래 스위치 상태로 응답할 수 있으니, 스위치를 다시 열기 전에 이전 Pod가 사라졌는지 본다.

```sh
kubectl -n target-dev exec deployment/demo-chat -c app -- python -c '
import json, os, sys, urllib.request as u
print(u.urlopen(u.Request("http://127.0.0.1:8080/admin/write-block", json.dumps({"blocked": sys.argv[1] == "true"}).encode(),
    {"Authorization": "Bearer " + os.environ["ADMIN_TOKEN"], "Content-Type": "application/json"}, method="PUT"), timeout=10).read())' false
```

### 재현

`testing/app_lab.py`가 실제 렌더 결과를 임시 k3s에 올려 위 표를 다시 잰다. 로컬 빌드는 이 시험의 매니페스트에만 넣고, 일반 렌더 경로에는 넣을 수 없다. 실행마다 고유 이름을 쓰고, 그 실행이 만든 컨테이너와 이미지만 ID와 라벨을 대조해 지운다.

```sh
infra/envs/target-workloads/testing/app_lab.py --demo-chat-src <demo-chat 체크아웃> --output <결과 파일>
```

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
