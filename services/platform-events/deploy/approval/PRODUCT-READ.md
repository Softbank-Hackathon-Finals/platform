# T38 인증 입력 조회 서비스

이 서비스는 기존 Actions OIDC로 T10의 현재 제품 입력을 읽는다. 사람 IdP가 필요한
승인 호스트와 따로 기동한다. 승인·등록·리허설 제출·권한 관리 HTTP는 열지 않는다.

```sh
uv sync --locked --no-dev --extra postgres --group product-inputs
python -m bravo_platform_events.product_read_server \
  --config /product-read/reader.json \
  --tls-cert /tls/tls.crt --tls-key /tls/tls.key
```

DB는 기존 `platform-db-rw.platform-state.svc`의 verify-full 연결과
`/credential/username`, `/credential/password`, `/ca/ca.crt` mount를 재사용한다.
`plans`와 `plan_commands`가 있는 schema **2 또는 3**을 받는다. DDL이나 migration은
실행하지 않는다. 환경·앱·계획 등록 API의 schema 3 요구는 그대로다. DB CA를 이
서비스의 HTTPS CA로 간주하지 않는다.

## 검토한 설정과 읽기 mount

`reader.json`은 아래 필드만 허용한다. 값은 실제 원문을 인수한 배포 담당자가 넣는다.
시작할 때 catalog·grant·tenant·계획·키를 만들지 않는다.

| 필드 | 인수할 값 |
|---|---|
| `version` | 정수 `1` |
| `tenant_id` | 현재 계획과 같은 실제 등록 tenant |
| `sts_provider` | 기존 STS 성공 응답의 정확한 `Provider`. issuer URL로 추정하지 않음 |
| `github_public_jwks_file` | 고정 GitHub issuer에서 TLS를 확인해 받은 공개 JWKS의 절대 경로 |
| `github_key_id` | 선택 필드. 생략하면 검토한 로컬 JWKS 전체에서 토큰의 `kid`로 선택. 지정하면 기존 단일 키 고정 |
| `catalog_root` | T15 운영 `TaskCatalog`의 서명된 등록본 디렉터리. 읽기 전용 |
| `catalog_key_file` | 해당 catalog 검증용 키의 절대 경로. T15 AttemptStore 서명 키와 별도 |

고정 issuer는 `https://token.actions.githubusercontent.com`, audience는
`sts.amazonaws.com`이다. subject는 기존 `infra/envs/subscription-runtime`의
immutable 조직·저장소 ID를 포함한 **develop 전용** 값이다. repository/owner ID도
대조한다. 선택한 RSA 공개 키로 기존 `WorkloadSender`의 서명·issuer·audience·subject·
만료·최대 수명 검증을 적용한다. private JWK·중복 선택 키·다른 알고리즘은 시작 전에
거부한다. `github_key_id` 생략 시 모든 키의 RSA·RS256·서명 용도와 고유한 `kid`를
시작 전에 검증한다. 토큰의 `kid`는 이 로컬 집합에서만 키를 선택하며, 없거나 모르는
키 ID는 403이다. 토큰의 키·키 URL 헤더도 거부한다. 키 교체는 검토한 mount/config
교체와 서비스 재시작이며 동적 URL 조회를 하지 않는다. 고정 키 방식도 계속 지원한다.

STS의 `Provider`, `Audience`, `SubjectFromWebIdentityToken` 전체 digest가 요청의
`principal`과 같아야 한다. JWT의 tenant·권한 주장은 사용하지 않는다. STS의 과거
성공 기록은 설정 근거이며 현재 JWT 인증 성공을 대신하지 않는다.

## 현재 권한과 HTTP

trusted 관리 경로는 `ProductInputAccess.set_grant`로 현재 계획의 principal·tenant·
용도 `t38-product-proposal`·유효기간·관리 결정 참조를 기록한다. 이전 revision을
대조하고 append-only로 저장한다. `enabled=False`는 회수다. HTTP나 JWT만으로 권한을
등록하지 않는다. T10 생산자와 이 조회 서비스는 같은 `ProductInputAccess.require`를
사용한다. 사람의 승인 권한으로 서비스 grant를 자동 변환하지 않는다.

```http
POST /internal/v1/product-inputs/read
Authorization: Bearer <current Actions OIDC>
Content-Type: application/json

{"principal":"<STS identity digest>","input_ref":"<registered reference>"}
```

T38는 STS 인증에 성공한 요청에서만 같은 원토큰을 메모리로 전달한다. 토큰을
worker·journal·설정·로그에 넣지 않는다. T10 Pod에서 Actions 토큰을 새로 발급할 수
있다고 가정하지 않으며, T10 workload JWT로 대신하지 않는다.

HTTPS와 정확한 두 필드만 받는다. 요청은 16 KiB, 응답은 16 MiB로 제한한다.
응답 상한은 정본의 64파일·파일당 UTF-8 32,768바이트가 JSON에서 escape되는 경우를
포함한다. 응답은 `admission`, `product_input`, `input_digest`, `principal`,
`purpose`, `expires_at` 6필드이며 `Cache-Control: no-store`다.

인증 실패는 403, 현재 입력·권한 거부는 409, 저장소 등 확인 불가는 503이다.
토큰은 DB 잠금 대기 뒤 다시 검증한다. 현재 plan·catalog·grant·입력 회수와 만료는
같은 계획 잠금에서 읽는다. 요청으로 source 파일·Admission·권한을 제출할 수 없다.
서버는 TLS 8443, worker 1개이며 forwarded header와 access log를 끈다.

## 실제 배포 전 인수

사설 HTTPS origin/host CA와 broker에서 그 주소로 가는 한정 경로, 기존 DB mount,
실제 운영 tenant/plan/catalog, 해당 STS principal의 현재 grant를 인수한다.
시험용 계획을 운영 계획으로 승격하지 않는다. 조회 서비스는 원본 source mount나
T15 AttemptStore HMAC 키를 받지 않는다. 이미 저장된 입력 바이트와 catalog를 읽으며
생산·등록 포트는 원본이 없어 거부한다.

별도 승인 호스트의 사람 인증·schema 3·단독 T15 실행 인수는 남는다. 이 서비스의
로컬 RSA/HTTP/DB 시험이나 배포가 모델 제안·검증 Job·T31 반영을 완료하지 않는다.
