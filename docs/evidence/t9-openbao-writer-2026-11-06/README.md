# T9 OpenBao 준비 상태와 리허설 쓰기 identity 로컬 실측

2026-11-06 02:11 KST, 커밋 `16d8483`의 렌더러로 측정했다. [readback](readback.json)은
비공개 원본 결과·실행 로그의 해시와 사례별 결과를 담는다. 토큰·합성 값은 기록하지 않았다.
[source-manifest](source-manifest.json)는 측정 커밋의 렌더러·점검 스크립트·시험 파일 해시다.

## 어디까지 확인했나

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

## 준비 상태 시간

차트 0.30.6의 `server.readinessProbe` 기본값은 `helm show values`로 timeout 3초, period 5초,
failure 2회임을 확인했다. 렌더 결과 StatefulSet은 exec `bao status -format=json`, timeout 10초,
period 10초, failure 3회(initialDelay 5초, success 1회는 차트 기본)다. 차트 원본의
`-tls-skip-verify` 명령은 최종 결과에 없다. 같은 차트 출력의 시간만 기본값으로 되돌리면
`finalize-bao`가 거부했다. 서버 dry-run은 PSS restricted 경고 없이 통과했다.

## 쓰기 identity 결과 (96/96 기대대로)

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

## 권한 상승 시험 결과

**상승이 실제로 가능했다.** 쓰기 토큰으로 본문이 `secret/data/bravo/target/*` 읽기인 정책
`rehearsal-app-reader-evil`을 만들 수 있었고, 모든 인자 제한을 지키는 역할
`rehearsal-app-reader-evil`(SA `rehearsal-eso-reader`, namespace `rehearsal-89ab…`)에 묶을 수 있었다.
그 namespace의 `rehearsal-eso-reader` 토큰(관리자 `kubectl create token`으로 발급)으로 로그인하면
`bravo/target/precutover/database`를 읽었다(HTTP 200, 값은 기록하지 않음).

쓰기 SA 혼자서는 쓰지 못했다. 같은 정책을 자기 SA·`rehearsal-control`에 묶는 역할은 거부됐고,
쓰기 SA 토큰으로 그 역할에 로그인하는 것도 거부됐다. 따라서 위험은 "쓰기 identity 탈취 +
rehearsal namespace에서 `rehearsal-eso-reader` 토큰을 얻을 수 있는 권한"의 조합이다. ACL은 정책
본문을 검사할 수 없으므로 현재 구조로는 막을 수 없다. 대응과 후속안(고정 슬롯 또는 namespace
템플릿 정책)은 [target-secrets README](../../../infra/envs/target-secrets/README.md#rehearsal-writer-agreed-with-t15-2026-11-06)와
[ADR-0027](../../adr/0027-rehearsal-openbao-writer.md)에 있다. 후속안은 시험하지 않았다.

## 재현

```sh
helm pull openbao --repo https://openbao.github.io/openbao-helm --version 0.30.6 --destination <새 디렉터리>
kind create cluster --name t9-writer --kubeconfig <임시 kubeconfig>
python3 infra/envs/target-secrets/testing/writer_acl_kind.py --kubeconfig <임시 kubeconfig> --output <비공개 경로>/writer-acl-results.json
kind delete cluster --name t9-writer
```

`--output`과 같은 디렉터리에 `finalize-bao` 출력 `bao.json`이 있으면 그 StatefulSet도 서버
dry-run한다. 이 점검은 `kind-*` 단일 노드 컨텍스트에서만 실행된다. 실행 뒤 kind 클러스터와
임시 kubeconfig·차트·출력 디렉터리를 지웠다.
