# T2 데모 머신 설치 결과 대조 — 2026-11-06

**ARM으로 바꾼 목적지 데모 VM이 공통 설치 스크립트의 결과와 같다.** 노드 안에서 `verify-node.sh` 12개가 모두 PASS였다. 여기에는 cloud-init 정상 종료, CIS sysctl, etcd 사용자, CIS 프로필 설정이 들어 있다. kube-bench는 80 PASS / 2 FAIL / 41 WARN / 7 INFO다. FAIL 2개(5.1.5, 5.1.6)는 노드 설치가 아니라 그 위에 올린 워크로드의 서비스 계정 설정에서 나왔다.

## 대상

| 항목 | 값 |
| --- | --- |
| 시각 | 2026-11-06 10:18~10:19 KST (verify-node 10:18:53, kube-bench 10:19:06에 끝남) |
| 노드 | 목적지 데모 VM 1대. 11/6 05:20쯤 x86 → ARM으로 전환된 뒤의 상태 |
| CPU | `arm64` (게스트 `uname -m` `aarch64`) |
| OS · 커널 | Ubuntu 24.04.5 LTS, `6.8.0-147-generic` |
| RKE2 | `v1.35.9+rke2r1` |
| RKE2 실행 인수 | `server --profile cis --write-kubeconfig-mode 0600 --disable rke2-ingress-nginx --disable rke2-traefik`. 공통 `bootstrap.sh`가 쓰는 `config.yaml`과 같다 |

노드 이름, 주소, 계정은 적지 않았다.

## 방법

운영자 Mac에서 목적지 API로만 했다. VM에 SSH로 들어가지 않았다.

1. 임시 namespace 하나를 만들고 `pod-security.kubernetes.io/enforce=privileged` 라벨을 붙였다. 클러스터 기본값은 `restricted`라 이 namespace에서만 노드 확인 Pod가 뜬다.
2. `kubectl debug node/<노드> --profile=sysadmin`으로 노드 확인 Pod를 띄우고 `chroot /host`로 저장소의 스크립트를 노드 root로 실행했다. 이미지는 노드에 이미 있던 앱 이미지를 썼고 새로 받지 않았다. 앱 이미지는 일반 사용자로 돌아 이 Pod만 root로 띄웠다.
3. 스크립트는 `source-manifest.json`의 커밋 그대로다. 결과는 Pod 로그로 읽었다.
4. 끝난 뒤 namespace를 UID 조건으로 지웠다. 노드에는 `run-kube-bench.sh`가 쓰는 `/var/log/kube-bench.json` 하나가 남는다.

앱 · DB · 복제 · 다른 namespace는 건드리지 않았다.

## 결과

### `verify-node.sh` (12 PASS / 0 FAIL)

| 확인 | 결과 |
| --- | --- |
| cloud-init 정상 종료 | PASS |
| CIS sysctl 적용 | PASS |
| etcd 사용자 · 그룹 | PASS |
| CIS 프로필 설정 | PASS |
| RKE2 버전 고정 | PASS |
| rke2-server 실행 | PASS |
| 노드 Ready | PASS |
| 특권 Pod 거부 | PASS |
| 감사 로그에 권한 변경 기록 | PASS |
| 감사 로그에 Secret 값 없음 | PASS |
| 절전 차단 | PASS |
| Tailscale 설치 | PASS |

11/3 기록([t2-dual-arch](../t2-dual-arch-2026-11-03/README.md))의 10개에 감사 로그 두 항목이 더해진 판이다.

### kube-bench 0.16.0, `rke2-cis-1.9` (80 PASS / 2 FAIL / 41 WARN / 7 INFO)

| FAIL | 내용 | 원인 |
| --- | --- | --- |
| 5.1.5 | 기본 서비스 계정을 쓰지 않을 것 | 클러스터에 올린 워크로드가 기본 서비스 계정을 쓴다. 예: `target-dev`의 앱 · 풀러 · SeaweedFS · Edge 터널과 시험 Job |
| 5.1.6 | 필요한 곳에만 서비스 계정 토큰을 마운트할 것 | 여러 namespace의 Pod가 토큰 자동 마운트를 끄지 않았다 |

둘 다 5.1(정책) 영역이다. 노드 · 컨트롤 플레인 · etcd 설치 항목에는 FAIL이 없다. 11/3 빈 클러스터에서는 82 PASS / 0 FAIL이었고, 그 차이 2개가 이 두 항목이다. 워크로드 쪽 조치는 이 기록의 범위가 아니다.

## 해석과 한계

- `cloud-init 정상 종료`와 CIS · etcd · 프로필 항목이 PASS라서, 이 VM은 공통 cloud-init(`infra/envs/onprem/render-user-data.sh` → `bootstrap.sh`)으로 설치된 상태와 같다고 본다.
- 노드 객체의 생성 시각은 11/3 그대로다. ARM VM으로 새로 설치한 뒤 클러스터 상태를 옮긴 것으로 보이지만, 설치 과정 자체는 보지 못했다(추정).
- 재부팅 뒤 재확인과 설치 시간은 재지 않았다.
- WARN 41개는 PASS로 합산하지 않는다. 읽는 법은 11/3 기록과 같다.
