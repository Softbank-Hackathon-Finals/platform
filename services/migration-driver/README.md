# T49 무인 해피패스 진행기 — 목적지 구축(P) 단계

T45 `target-onprem` 스택을 사람 손 없이 끝까지 돌린다. T45가 사람 단계(`operator_action`)에서 멈추면 이 패키지가 같은 일을 코드로 하고 다시 재개한다. T45는 자기 확인 조건으로 결과를 관찰해 `SKIPPED`로 넘긴다. 스택의 단계 모델은 바꾸지 않고, 승인된 실행 순서 밖에서 먼저 손대는 일도 없다.

| 단계 | 플랫폼이 하는 일 |
|---|---|
| `host-dirs` | 사이트 호스트가 선언된 디렉터리를 만들고 노드에 `bravo.io/host-paths` 주석을 남긴다. 데이터가 있는 디렉터리는 다시 쓰지 않는다 |
| `openbao-image` · `app-image` | 검사한 OCI 묶음을 태그와 `@sha256` 별칭으로 반입한다. 앱은 `imagePullPolicy: Never`라 amd64도 고정 GHCR index를 그대로 보존한 OCI(`skopeo copy --all`)로 반입한다. 목적지는 GHCR 자격을 갖지 않는다 |
| `argocd` | T44 고객 Argo CD(`infra/iac/argocd`)를 설치한다(Application 없음) |
| `config-repo-access` | 목적지 설정 저장소를 읽기 전용으로 등록한다. GitHub 저장소(ADR-0033: 목적지마다 비공개 저장소 하나)는 플랫폼이 Ed25519 deploy key를 한 번 만들어 보관소에 두고, 공개키만 읽기 전용으로 등록한 뒤 개인키를 Argo CD repository Secret에만 넣는다(`credential: deploy-key`) |
| `object-secrets` | 계획에 묶인 T44 원하는 상태(`desired`)로 `s3.json`을 렌더해 넣는다 |
| `openbao-tls` · `openbao-init` | OpenBao CA·leaf, init 1회, unseal, KV v2(CAS), Kubernetes 인증·정책, 출발지 값(CAS 0) |
| `app-metrics-client` | 목적지 앱 CA로 T37 앱 지표 클라이언트 인증서를 발급한다 |

복구 자료(init 응답, CA 키, 객체 저장소 신원)는 플랫폼 보관소(`vault.py`)에만 둔다(0600, 한 번 쓰면 바꾸지 않음). 비밀값은 stdin으로만 전달한다.

실행 중 사람이 개입했던 오류 대응도 코드로 넣었다.
- 어댑터 오류는 원인을 기록하고 세 번까지 재관찰한다.
- T45가 미완료 OpenBao 초기화를 먼저 통과시킨 경우 이 패키지가 마저 끝낸다.
- 출발지 대역 DB를 다시 만들면 풀러와 앱을 재시작한다.

## 위치와 남은 일

- 11/7 배치 기준: 플랫폼 구성요소는 AWS 플랫폼 RKE2에만 둔다. 이 패키지의 `Destination`은 플랫폼 코드이며, 다음 단계에서 T10 workflow host의 단계로 올린다.
- `e2e/`는 개발용 격리 하네스다(Mac에서 실제 T10 승인 코드와 로컬 IdP, 로컬 설정 저장소, 같은 클러스터 안의 출발지 대역). 최종 실행에는 쓰지 않는다.
- 사이트 호스트 어댑터(`host.py`): `LocalVmHost`(`infra/envs/onprem/local-vm.sh`), `GceRke2Host`(`infra/envs/gcp-rke2/node.sh`, IAP·OS Login). `destination.json`의 `host.kind: gce`로 고른다.

## 실측 (2026-11-07, 격리 VM, arm64/HVF)

새 VM에서 사람 확인 0회로 `SUCCEEDED`(6차, 58초)를 얻었다. 결과: 35단계 중 적용 14, 관찰로 `SKIPPED` 18, Tailscale 줄기 `OMITTED` 3. `verify`를 통과했고 Argo Application 8개가 Synced/Healthy, demo-chat 1/1, CNPG healthy였다. tailnet 합류(T48 WIF)는 아직 없어 `tailnet=none`으로 돌렸다.

```sh
uv sync --group e2e
cd e2e && ../.venv/bin/python t10_host.py <t10-config.json> &        # 실제 T10 승인 코드, 127.0.0.1
../.venv/bin/python source_fixture.py up <destination.json>           # 같은 클러스터 안의 출발지 대역
../.venv/bin/python run_destination.py <destination.json> <run-id>   # 계획 → 승인 → T45 실행 → 플랫폼 동작
```
