# T10 전용 창 수신부

`POST /v1/observability/windows`로 받은 T18 창을 기존 T10 계획 트랜잭션에 저장한다. 형식·통계·시각 검사는 고정한 T36 `24b3cf51`의 코드가 맡는다. 이 서버는 콘솔 API, 계획 등록, binding 관리, SHIFT 판정이나 Executor 실행을 제공하지 않는다.

| 구간 | 연결과 권한 |
|---|---|
| 생산자 | `tag:bravo-loadgen`, 전용 Bearer, 합의한 계획 하나. DB 자격 없음 |
| Tailnet | `tag:bravo-window-intake`의 TCP 30981만 허용. HTTP 내용은 Tailscale 전송으로 암호화 |
| Kubernetes | 기존 T7 Operator의 전용 proxy Pod만 수신 Pod의 8080에 접근. 공인 LB·NodePort 없음 |
| 수신기 | API 토큰 없는 전용 ServiceAccount, 읽기 전용 root, 비루트 실행. Secret에는 토큰 해시와 서버 소유 identity |
| DB | 기존 runtime 로그인·`verify-full` CA. 같은 계획 잠금 안에서 자격 재검사·권한 확인·저장·commit |
| migration | 별도 migration 로그인 Job. HTTP 서버 시작 시 DDL 없음 |

Tailscale Operator의 [서비스별 태그](https://tailscale.com/docs/kubernetes-operator/reference/tags)를 사용한다. `tailnet-fragment.json`은 기존 정책에 합칠 **추가분**이다. 전체 정책을 이 파일로 교체하지 않는다. 현재 정책을 다시 읽고 기존 항목 보존과 새 allow/deny tests를 확인한 뒤 저장·재조회한다. receiver 태그를 이미 만든 Service에서 바꾸려면 공식 문서의 재생성 조건을 따른다.

## 빌드와 배포

```sh
cd services/platform-events
uv sync --locked --all-extras
uv run pytest tests/test_window_api.py tests/test_window_store.py -q
uv run python deploy/window/build.py /private/operator-output/window-build
```

`build.py`는 `uv.lock`과 upstream commit을 확인하고 소스·패키지·잠긴 wheel 목록만 허용한 임시 context를 만든다. base image는 digest 고정이다. 결과 이미지를 네트워크 차단·읽기 전용·비루트로 실행해 import까지 확인한 뒤 OCI archive와 해시 영수증을 만든다. 로컬 홈·`.env`·Git 자격·Kubernetes Secret은 context에 넣지 않는다.

현재는 개발용 단일 RKE2 노드의 검증된 OCI cache를 사용한다. `import_host.py`를 운영자 SSM 경로로 실행하고 localhost 18981에만 포트 포워딩한다. 입력은 archive SHA-256과 크기이며, 같은 digest의 URL에 archive를 PUT한다. 크기·해시 일치 후 containerd에 import하고 임시 파일을 지운다. 이 과정은 registry 권한이나 credential provider를 바꾸지 않는다. 전송 후 자기 SSM 세션을 종료하고 이미지 영수증을 보존한다.

`imagePullPolicy: Never`이므로 새 노드나 cache 유실 시 재import가 필요하다. 지속 registry 배포·멀티노드·HA를 완료했다고 보지 않는다. T12 전용 ECR 게시 역할을 재사용하지 않는다.

배포 순서는 다음과 같다.

1. T7 담당과 변경 창을 맞추고 현재 DB 소비자·스키마·암호화 백업/격리 복구를 확인한다. v1 전용 소비자가 있으면 먼저 호환 버전으로 바꾼다.
2. 이미지 영수증을 확인하고 `render.py --image <source-bound-image>`로 전용 자원을 렌더한다. 서버 측 dry run을 먼저 수행한다. 기존 T7 정책·DB·Operator는 다시 적용하지 않는다.
3. `jobs.job(image, "migrate")`의 별도 Job을 실행하고 **성공을 확인한 뒤** 다음 단계로 간다. 0002는 v1에서만 허용된다. 실패·응답 유실은 스키마를 다시 읽고 판정하며 무조건 재실행하지 않는다.
4. T18 담당과 실제 계획·생산자·만료·Secret 전달 경로를 맞춘다. 인증된 운영 호스트에서 `issue_credential.py`에 비밀이 아닌 identity metadata만 stdin으로 준다. 생성된 원문 토큰을 로그·파일·Slack·명령줄로 추출하지 않는다. 승인된 생산자 Secret 전달 경로가 없으면 실제 전달을 보류한다.
5. 관리 권한을 확인한 내부 호스트에서 `WindowIntake.set_binding`으로 현재 계획과 결속한다. `administration_ref` 문자열만으로 권한이 생기지 않는다. 이 저장 API를 외부 endpoint로 열지 않는다.
6. 수신 Deployment Ready와 실제 tailnet HTTP 수신·DB readback·권한 거부를 확인한다. T18 `--window-url`과 `BRAVO_WINDOW_TOKEN`은 기존 생산 코드 계약을 유지한다.

`jobs.job(image, "enroll")`는 `t10-ingress-probe-*`의 합성 계획에만 쓸 수 있는 검증 도구다. 실제 T18 계획을 자동 생성하거나 권한을 주지 않는다. 운영용 연결을 완료하려면 실제 계획과 생산자 쪽 전달 경로의 인수가 별도로 필요하다.

## 스키마 3 등록 테이블 인수

`jobs.job(image, "migrate", schema_version=3)`은 저장소의 `0003_registrations.sql`을 별도 migration 로그인으로 한 번 실행한다. 기본값은 기존 v1→v2 작업이다. SQL과 실행기 전체의 해시로 immutable ConfigMap 이름을 정하므로 두 버전의 내용을 혼용하지 않는다. 잠금은 최대 5초, SQL은 최대 60초 기다리며, Job은 자동 재시도하지 않는다. 실패나 응답 유실 때는 현재 스키마·테이블·Job UID를 먼저 재조회한다.

운영 전환 순서는 다음과 같다.

1. 현재 DB 소비 프로세스를 조사한다. 특히 기존 창 수신 이미지의 `PostgresPlanStore`와 `window_server`가 v3를 허용하는지 실제 실행 파일의 해시로 확인한다. 저장소 코드만 확인해서는 배포된 소비자의 호환성을 증명할 수 없다.
2. 현재 상태의 암호화 백업을 호스트 밖에 보존하고 네트워크가 차단된 PostgreSQL에 복원한다. 데이터·역할·권한·제약·시퀀스를 비교하고, 복원본에서 migration 로그인으로 2→3 전환과 중복 실행 거부를 시험한다. 역할 암호나 DB 접속값은 증빙에 포함하지 않는다.
3. 검토된 v2/v3 호환 창 수신 이미지를 먼저 올린다. 기존 Deployment의 이미지 필드만 현재 UID·resourceVersion·이전 이미지에 결속해 갱신한다. 생산자 identity, Service, Secret, 네트워크 정책을 다시 만들지 않는다. Ready와 새 DB 연결을 확인한다.
4. 같은 운영 창에서 원본 스키마가 여전히 2인지, 백업 이후 데이터 변화가 없는지 재조회한 뒤 migration Job을 만든다. 기존 데이터가 바뀌었으면 백업·복원 확인을 갱신한다. 별도 migration Secret과 CA만 mount하며 사람·생산자 토큰은 사용하지 않는다.
5. 스키마 3, 빈 등록 테이블, 기존 행 보존, runtime의 SELECT/INSERT만 허용됨을 확인한다. 호환 수신 Pod에서 새 연결과 `/readyz`도 확인한다. 생성한 Job·ConfigMap은 UID를 확인해 정리한다.

스키마 3 적용은 실제 환경·앱·계획 등록이나 운영 인수 완료를 뜻하지 않는다. 실제 등록은 [T10 등록 계약](../../../../docs/contracts/T10-registration.md)의 사람 인증·서버 소유 tenant 권한·실제 연결과 릴리스 대조를 거친다. `enroll`의 합성 계획을 운영 계획으로 승격하거나 서비스 OIDC를 사람 신원으로 사용하지 않는다.

## 토큰 교체와 회수

receiver Secret `t10-window-producer-identity`는 `producer.json`에 `token_sha256`과 `ProducerIdentity`를 둔다. 원문은 별도 `t10-window-producer-token` Secret의 `token` 키에만 둔다. 수신 Pod에는 원문 Secret을 mount하지 않는다. `issue_credential.py`는 최대 8시간의 최초 발급만 하며 기존 Secret을 덮어쓰지 않는다.

즉시 회수는 기존 계획 잠금을 사용하는 `set_binding(enabled=False, expected_revision=...)`으로 처리한다. Kubernetes Secret volume 갱신에는 지연이 있으므로 파일 교체만으로 즉시 회수됐다고 보고하지 않는다. 교체 시 이전 binding을 먼저 비활성화하고 새 credential ID·해시·생산자 토큰을 배치한 뒤, 현재 계획·revision을 확인해 새 binding을 활성화한다. 이전 활성화 명령 재시도는 과거 영수증만 반환한다. 계획 version/state/digest가 바뀌어도 새 binding이 필요하다.

## 검증과 정리

`verify_cloud.py`는 임시 `tag:bravo-loadgen` proxy에서 실제 tailnet IP·30981·HTTP·운영 DB 경로를 시험한다. 정상 201, 중복 409, 잘못된 토큰 401, 다른 계획 404, 회수 후 403, 다른 태그 차단을 확인한다. 수신 Pod만 재시작해 창 보존을 읽고 합성 binding을 비활성화한다. 원래 창의 입력값과 저장값을 비교하며 저장 시각은 서버 값인지 확인한다.

이 시험은 T18 담당자의 별도 EC2, 실제 앱 부하 또는 SHIFT/PASS의 증명이 아니다. 201 역시 SLI 판정 성공을 뜻하지 않는다. 각 측정의 범위는 [증빙](../../../../docs/evidence/t10-window-ingress-2026-11-04/README.md)에 기록한다.

시험 후 자신이 만든 Job·ConfigMap·ProxyGroup·생산자 원문 Secret을 UID와 소유 범위로 확인해 정리한다. 기존 DB, Operator, cutover proxy, 다른 작업의 Pod는 지우지 않는다. 합성 계획·창·append-only 관리 영수증은 회수된 상태로 보존하며 DDL로 강제 삭제하지 않는다. 수신 Deployment·Service·정책과 해시 identity Secret은 실제 인수 대기 상태로 유지할 수 있다.
