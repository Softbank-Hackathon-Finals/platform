# T9 OpenBao 준비 상태와 리허설 쓰기 identity 로컬 실측

이 폴더에는 측정이 두 번 있다.

- **A안 재실측** (2026-11-06 03:47 KST, 커밋 `12d6b4f`): 쓰기 identity에서 정책 쓰기 권한을 없애고
  운영자 소유 namespace 템플릿 정책 하나로 바꾼 뒤의 측정. [readback-aplan](readback-aplan.json),
  [source-manifest-aplan](source-manifest-aplan.json). 아래 첫 절.
- **이전 설계 측정** (02:11 KST, 커밋 `16d8483`): 권한 상승이 실제로 가능함을 확인한 측정.
  [readback](readback.json), [source-manifest](source-manifest.json). 이력으로 그대로 둔다.

두 측정 모두 **로컬 kind 클러스터와 upstream OpenBao 2.7.2 dev 서버**에서 했다. 데모 목적지(RKE2,
TLS, 팀 패치 이미지)가 아니다. readback은 비공개 원본 결과·실행 로그의 해시와 사례별 결과만 담는다.
토큰·합성 값·mount accessor 값은 기록하지 않았다.

## A안 재실측

kind 클러스터 `t9-aplan`(kind v0.32.0, Kubernetes v1.36.1, 단일 노드, kindnet NetworkPolicy)에
`quay.io/openbao/openbao:2.7.2`(digest는 readback) dev 서버를 `target-openbao` 대역으로 띄우고,
렌더러 출력(정책·역할·NetworkPolicy)을 바꾸지 않고 적용했다. 로그인은 모두 실제 ServiceAccount
토큰과 Kubernetes TokenReview로 했다. **157개 사례 모두 기대대로**였다. 쓰기 identity의 관리 namespace는
T15 담당과 합의한 `bravo-rehearsal-control`이다(이전 측정의 `rehearsal-control`에서 바뀜). 설계 기록은
[ADR-0029](../../adr/0029-rehearsal-openbao-writer.md).

### 어디까지 확인했나

| 범위 | 실행 위치 | 상태 |
| --- | --- | --- |
| 운영자 설치: `auth enable` 뒤 `bao read -field=accessor sys/auth/kubernetes`로 accessor 읽기 → `rehearsal-reader-policy` 렌더 → 정책 쓰기 | kind 안 dev 서버, 실제 `bao` CLI와 HTTP API | 확인 (CLI 값 = `sys/auth` 목록 값, 형식 `auth_kubernetes_<8 hex>`, 저장된 정책 = 렌더 결과) |
| 쓰기 identity의 허용·거부, 이전 상승 경로 재현 | 같은 서버, 실제 SA 토큰 | 확인 |
| namespace 템플릿 reader의 격리 | 같은 서버, `rehearsal-aaa`·`rehearsal-bbb` 실제 SA 토큰 | 확인 |
| 정상 경로: 라벨 붙은 쓰기 Job → 렌더된 init container가 `credentials.json` 생성 → 정리 Job | kind Pod·Job | 확인 (init container는 dev 대역에 맞춰 주소·CA 변수·이미지만 바꿈, reader 네트워크 peer는 시험 전용) |
| 렌더된 Namespace·PV/PVC·RBAC·NetworkPolicy·쓰기 identity·`finalize-bao` StatefulSet 서버 dry-run | 같은 kind | 확인 |
| 데모 목적지 RKE2, TLS·pebbledb·팀 패치 이미지(`2.7.2-bravo.3-nonroot`) | — | **미적용·미검증** |
| T15 실제 준비 Job, rehearsal 시도 namespace의 reader 네트워크 peer | — | **미검증·미구현** (시험 Job과 시험 전용 peer로 대신함) |

### 결과 (157/157 기대대로)

| 묶음 | 관측 |
| --- | --- |
| 운영자 설치 | accessor를 CLI(`bao read -field=accessor sys/auth/kubernetes`)와 API 목록에서 같은 값으로 읽음. 렌더한 `rehearsal-app-reader` 정책이 그대로 저장됨 |
| 쓰기 로그인 | `bravo-rehearsal-control/rehearsal-openbao-writer` + audience `openbao`만 성공. 기본 audience, 다른 SA, `bravo-rehearsal-control`의 `rehearsal-eso-reader` 거부. 토큰 정책 `rehearsal-openbao-writer` 하나, TTL 300초 |
| 쓰기 허용 | `bravo/rehearsal/<namespace>/database` 쓰기(CAS 0)·읽기·메타데이터 갱신·조회·삭제, 렌더된 reader 역할 본문으로 `rehearsal-app-reader-<namespace>` 쓰기·삭제, 자기 토큰 조회·갱신·폐기 |
| **(1) 정책 경로 전부 거부** | `sys/policies/acl/rehearsal-app-reader` 쓰기·읽기·삭제, `rehearsal-app-reader-<namespace>`·`default`·자기 정책·`target-app-reader` 쓰기, 구형 `sys/policy`, `LIST sys/policies/acl`, `sys/policies/password` |
| **(1) `token_policies` 제한** | `rehearsal-app-reader-evil`, `rehearsal-app-reader-<namespace>`, `rehearsal-app-reader*`, 뒤 공백, `rehearsal-app-reader,default`, JSON 목록 `["rehearsal-app-reader"]`, `default`, 쓰기 정책, `target-app-reader`, 값 누락, 구형 `policies` 모두 거부 |
| **(3) namespace 제한** | 관리 namespace `bravo-rehearsal-control`, `target-dev`, `bravo-secrets`, `default`, `kube-system`, 옛 이름 `rehearsal-control`·`rehearsal-control-x`·`rehearsal-control `, `rehearsal-aaa `, 쉼표 목록(다른 namespace 포함·rehearsal 둘), `rehearsal-*`, `*`, JSON 목록, selector, 값 누락 모두 거부 |
| 기타 역할 인자 | SA 쓰기 계정·쉼표·`*`, audience 누락·다른 값, 긴 TTL, default 정책 켬, `token_period`, `token_bound_cidrs`, `alias_name_source` 거부. 역할 읽기는 `required_parameters` 때문에 거부(이전과 같음) |
| 경로 거부 | `bravo/target/*` 읽기·쓰기·메타데이터, `bravo/rehearsal-evil`, destroy, list, 자기 역할·`target-app-reader` 역할, `sys/auth`·`sys/auth/kubernetes`·`sys/mounts`·kubernetes config·토큰 생성·`identity/entity`·`identity/entity-alias` |
| `..` 경로 | HTTP 307로 정리된 `bravo/target` 경로를 알려 주고 값은 주지 않음. 따라가면 거부 |
| **이전 상승 경로 재현** | 본문이 `bravo/target/*` 읽기인 `rehearsal-app-reader-evil` 정책 쓰기 **거부**, 그 정책을 `rehearsal-bbb`의 reader SA나 쓰기 SA에 묶는 역할 쓰기 **거부**, 그 역할 로그인 **거부**, 서버에 그 정책 없음(404) |
| **(2) reader 격리** | `rehearsal-aaa` reader: 자기 역할 로그인 성공, 토큰 정책 `rehearsal-app-reader` 하나·TTL 300초. `bravo/rehearsal/rehearsal-aaa/database` 읽기 **허용**. `rehearsal-bbb`, 접두 형제 `rehearsal-aaab`, subtree 루트, `bravo-rehearsal-control` 경로, `bravo/target/precutover/database`·`bravo/target/objects` 읽기 **거부**. 쓰기·메타데이터·list·정책 읽기·역할 쓰기·토큰 생성 거부. `rehearsal-bbb` reader도 대칭(자기 것만 허용) |
| 역할-namespace 바인딩 | A 역할에 B·`bravo-rehearsal-control`·`target-dev`의 `rehearsal-eso-reader` 로그인 거부, B 역할에 A 거부, 기본 audience 거부 |
| 이름은 권한이 아님 | 다른 이름(`rehearsal-app-reader-crossname`)에 같은 제약으로 A를 묶어도 그 토큰은 A 경로만 읽고 B는 거부 |
| 잘못된 accessor | 같은 템플릿을 틀린 accessor로 렌더한 정책을 root가 붙인 역할: 로그인은 되지만 자기 경로 읽기 **거부**(규칙이 풀리지 않아 건너뜀, 실패 시 닫힘) |
| 정리 | 쓰기 토큰으로 역할 셋·KV 메타데이터 셋 삭제, 폐기 뒤 토큰 사용 거부 |
| **(4) Pod 정상 경로** | `bravo-rehearsal-control`의 라벨 쓰기 Job이 projected 토큰으로 로그인 → `rehearsal-aaa` 경로 쓰기·읽기, 역할 쓰기 성공, `bravo/target` 읽기와 `rehearsal-app-reader`·`rehearsal-app-reader-evil` 정책 쓰기 거부, 폐기. `rehearsal-aaa`의 렌더된 init container가 로그인해 `credentials.json`(모드 400, 임시 파일 없음)을 만들고 Pod `Succeeded`. 정리 Job이 역할과 KV 메타데이터를 지움 → 역할·값 404, reader 재로그인 거부, 운영자 정책은 남음 |
| reader 네트워크 | 렌더된 `network` 정책만 있으면 init container는 `context deadline exceeded`로 실패(Pod `Failed`). 시험 전용 peer(`rehearsal-aaa` + Pod 라벨)를 더한 뒤에만 성공. 렌더 결과에는 reader peer가 여전히 없다 |
| 쓰기 네트워크 | 라벨 `app.kubernetes.io/component=rehearsal-openbao-writer`가 있는 `bravo-rehearsal-control` Pod는 연결(종료 0). 같은 namespace의 라벨 없는 Pod, `rehearsal-bbb`와 옛 이름 `rehearsal-control`의 라벨 Pod는 차단(종료 1) |

Pod 단계의 `deny`는 셸 명령의 실패이고, HTTP 단계의 `deny`는 403만 뜻한다(다른 오류는
`allow-but-backend-*`로 남는다). init container에 바꾼 것은 `https://…:8200` → `http://…:8200`,
`BAO_CACERT` 제거, 이미지(upstream), `BAO_CLIENT_TIMEOUT=10s`와 PSS용 seccomp 추가뿐이다. 로그인
역할·KV 경로·임시 파일·원자적 교체·폐기 로직은 렌더 결과 그대로다.

### 소스 확인

OpenBao 커밋 `9e0a0d3`(2.7.2)에서 확인했다. Kubernetes 인증은 내장
(`internal/builtin/credential/kubernetes`)이고, 로그인 시 `logical.Alias.Metadata`에
`service_account_namespace`를 넣는다(`path_login.go`). 이 값은 TokenReview 응답의 namespace와
JWT 값이 같을 때만 쓰인다. ACL 경로 템플릿은 `sdk/helper/identitytpl`의
`identity.entity.aliases.<mount accessor>.metadata.<key>`로 풀리며, 풀리지 않으면
`internal/vault/policy/policy.go`가 그 규칙을 건너뛴다. accessor 형식은 `<type>_<8 hex>`
(`internal/vault/mount.go`). 역할의 `alias_name_source` 기본값은 SA UID라 namespace마다 다른
entity가 된다.

### 재현

```sh
helm pull openbao --repo https://openbao.github.io/openbao-helm --version 0.30.6 --destination <새 디렉터리>
kind create cluster --name t9-aplan --kubeconfig <임시 kubeconfig>
python3 infra/envs/target-secrets/testing/writer_acl_kind.py --kubeconfig <임시 kubeconfig> --output <비공개 경로>/writer-acl-results.json
kind delete cluster --name t9-aplan
```

`--output`과 같은 디렉터리에 `finalize-bao` 출력 `bao.json`이 있으면 그 StatefulSet도 서버
dry-run한다(이번에는 `--kube-version 1.36.1`로 렌더). 실행 뒤 kind 클러스터 `t9-aplan`을 지웠다.
비공개 원본(결과·실행 로그)은 저장소 밖 접근 제한 경로에 남겼다.

## 이전 설계 측정 (이력, 커밋 `16d8483`)

아래는 A안 이전 설계(쓰기 identity가 `rehearsal-app-reader-*` 정책을 쓸 수 있던 때)의 측정을
그대로 옮긴 것이다. 권한 상승 사례의 **성공**이 A안을 결정한 근거다. 당시 관리 namespace 이름은 `rehearsal-control`이었다.

2026-11-06 02:11 KST, 커밋 `16d8483`의 렌더러로 측정했다. [readback](readback.json)은
비공개 원본 결과·실행 로그의 해시와 사례별 결과를 담는다. 토큰·합성 값은 기록하지 않았다.
[source-manifest](source-manifest.json)는 측정 커밋의 렌더러·점검 스크립트·시험 파일 해시다.

### 어디까지 확인했나

| 범위 | 실행 위치 | 상태 |
| --- | --- | --- |
| 고정 차트 0.30.6 + `bao-values`의 `helm template` → `finalize-bao` | 로컬 Helm 4.3.1, 차트 SHA-256 고정값과 일치 | 확인 |
| 렌더된 Namespace·PV/PVC·RBAC·NetworkPolicy·쓰기 identity·최종 StatefulSet의 `kubectl apply --dry-run=server` | 로컬 kind v0.32.0, Kubernetes v1.36.1 | 확인 |
| 쓰기 정책·역할의 허용/거부, 권한 상승 사례 | kind 안의 upstream OpenBao 2.7.2 dev 서버, 실제 ServiceAccount 토큰과 TokenReview | 확인 |
| `rehearsal-control` 라벨 Job → OpenBao 8200 네트워크 경로 | kind 기본 CNI(kindnet)의 NetworkPolicy 집행 | 확인 |
| 데모 목적지 RKE2, TLS·pebbledb·팀 패치 이미지(`2.7.2-bravo.3-nonroot`) | — | **미적용·미검증** |
| 목적지에서 `bao status`가 10초 안에 끝나 Ready가 안정되는지 | — | **미검증** (흔들림 관측은 팀원 보고) |
| T15 실제 준비 Job | — | **미검증** (시험 Job으로 대신함) |

OpenBao는 `quay.io/openbao/openbao:2.7.2`(digest는 readback)다. 팀 이미지와 같은 upstream
커밋 `9e0a0d3`이며, 팀 이미지는 go-jose 패치만 다르다. dev 모드라 HTTP·메모리 저장·unseal 상태다.
ACL·Kubernetes 인증 동작은 같은 소스이지만, 목적지에서 같은 시험을 다시 해야 한다.

### 준비 상태 시간

차트 0.30.6의 `server.readinessProbe` 기본값은 `helm show values`로 timeout 3초, period 5초,
failure 2회임을 확인했다. 렌더 결과 StatefulSet은 exec `bao status -format=json`, timeout 10초,
period 10초, failure 3회(initialDelay 5초, success 1회는 차트 기본)다. 차트 원본의
`-tls-skip-verify` 명령은 최종 결과에 없다. 같은 차트 출력의 시간만 기본값으로 되돌리면
`finalize-bao`가 거부했다. 서버 dry-run은 PSS restricted 경고 없이 통과했다.

### 쓰기 identity 결과 (96/96 기대대로)

| 묶음 | 관측 |
| --- | --- |
| 로그인 | `rehearsal-control/rehearsal-openbao-writer` + audience `openbao`만 성공. 기본 audience 토큰, 다른 SA 토큰 거부. 토큰 정책은 `rehearsal-openbao-writer` 하나, TTL 300초 |
| 허용 | `bravo/rehearsal/*` KV 쓰기·읽기·메타데이터 갱신·조회·삭제, `rehearsal-app-reader-*` 정책 쓰기·읽기·삭제, 렌더된 reader 역할 본문 쓰기·삭제, 자기 토큰 조회·갱신·폐기 |
| 경로 거부 | `bravo/target/*` 읽기·쓰기·메타데이터, `bravo/rehearsal-evil`, destroy, list, `default`·자기 정책·`target-app-reader`·대시 없는 이름·구형 `sys/policy`, 자기 역할·`target-app-reader` 역할, `sys/auth`·`sys/mounts`·kubernetes config·토큰 생성·identity |
| 역할 인자 거부 | namespace `target-dev`·`rehearsal-control`·뒤 공백·쉼표 목록·glob·`*`·JSON 목록·selector, SA 쓰기 계정·쉼표·`*`, audience 누락·다른 값, 긴 TTL, default 정책, 정책 `default`·쓰기 정책·쉼표, 구형 `policies`, `token_period`, `token_bound_cidrs` |
| 역할 읽기 | 거부. OpenBao 2.7.2가 read에도 `required_parameters`를 적용해서다. 그래서 정책에서 read를 빼고 README에 적었다 |
| `..` 경로 | `secret/data/bravo/rehearsal/../target/...`는 HTTP 307로 정리된 경로로 보내고 값을 주지 않았다. 따라가면 `bravo/target` 요청이라 거부 |
| reader | 바인딩한 namespace의 `rehearsal-eso-reader`만 로그인. 자기 실행 경로 읽기만 허용, 다른 실행·`bravo/target`·쓰기 거부 |
| 네트워크 | 라벨 `app.kubernetes.io/component=rehearsal-openbao-writer`가 있는 `rehearsal-control` Pod는 연결(종료 0). 같은 namespace의 라벨 없는 Pod와, 다른 rehearsal namespace의 라벨 Pod는 차단(종료 1) |
| Pod 경로 | 라벨 Job이 projected 토큰(audience `openbao`)으로 로그인해 KV 쓰기·읽기, reader 정책·역할 생성·삭제, 메타데이터 삭제, 토큰 폐기에 성공. `bravo/target` 읽기는 거부 |

### 권한 상승 시험 결과

**상승이 실제로 가능했다.** 쓰기 토큰으로 본문이 `secret/data/bravo/target/*` 읽기인 정책
`rehearsal-app-reader-evil`을 만들 수 있었고, 모든 인자 제한을 지키는 역할
`rehearsal-app-reader-evil`(SA `rehearsal-eso-reader`, namespace `rehearsal-89ab…`)에 묶을 수 있었다.
그 namespace의 `rehearsal-eso-reader` 토큰(관리자 `kubectl create token`으로 발급)으로 로그인하면
`bravo/target/precutover/database`를 읽었다(HTTP 200, 값은 기록하지 않음).

쓰기 SA 혼자서는 쓰지 못했다. 같은 정책을 자기 SA·`rehearsal-control`에 묶는 역할은 거부됐고,
쓰기 SA 토큰으로 그 역할에 로그인하는 것도 거부됐다. 따라서 위험은 "쓰기 identity 탈취 +
rehearsal namespace에서 `rehearsal-eso-reader` 토큰을 얻을 수 있는 권한"의 조합이다. ACL은 정책
본문을 검사할 수 없으므로 현재 구조로는 막을 수 없다. 대응과 후속안(고정 슬롯 또는 namespace
템플릿 정책)은 [target-secrets README](../../../infra/envs/target-secrets/README.md)와
[ADR-0029](../../adr/0029-rehearsal-openbao-writer.md)에 있다. 후속안은 이 측정에서 시험하지 않았다(이후 A안 재실측).

### 재현

```sh
helm pull openbao --repo https://openbao.github.io/openbao-helm --version 0.30.6 --destination <새 디렉터리>
kind create cluster --name t9-writer --kubeconfig <임시 kubeconfig>
python3 infra/envs/target-secrets/testing/writer_acl_kind.py --kubeconfig <임시 kubeconfig> --output <비공개 경로>/writer-acl-results.json
kind delete cluster --name t9-writer
```

`--output`과 같은 디렉터리에 `finalize-bao` 출력 `bao.json`이 있으면 그 StatefulSet도 서버
dry-run한다. 이 점검은 `kind-*` 단일 노드 컨텍스트에서만 실행된다. 실행 뒤 kind 클러스터와
임시 kubeconfig·차트·출력 디렉터리를 지웠다.
