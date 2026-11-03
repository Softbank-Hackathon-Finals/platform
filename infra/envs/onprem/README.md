# T2 온프렘 RKE2

빈 Ubuntu 24.04 VM에 user-data 하나를 넣으면 RKE2가 CIS 프로필로 뜬다. 온프렘 데모 머신이 정해지기 전까지는 Apple Silicon Mac의 로컬 VM으로 같은 과정을 시험한다. 측정 범위는 [증거 기록](../../../docs/evidence/t2-onprem-rke2-2026-11-03/README.md)에 있다.

| 파일 | 하는 일 |
|---|---|
| [`render-user-data.sh`](render-user-data.sh) | 공통 [`bootstrap.sh`](../../shared/rke2/bootstrap.sh)에 Tailscale 설치를 더해 user-data를 출력한다 |
| [`local-vm.sh`](local-vm.sh) | Mac에서 새 VM을 만들고 지운다. x86 머신에서는 쓰지 않는다 |
| [`verify-node.sh`](verify-node.sh) | 노드가 기준대로 섰는지 10개 항목을 확인한다 |
| [`run-kube-bench.sh`](run-kube-bench.sh) | kube-bench로 CIS 점검을 돌리고 요약을 출력한다 |

## 쓰는 법

로컬 VM은 `brew install qemu` 뒤 아래 순서로 쓴다. VM이 켜져 있는 동안 메모리 8GB를 차지한다.

```sh
./local-vm.sh up                    # 새 VM 생성. cloud-init이 끝날 때까지 기다린다
./local-vm.sh run verify-node.sh    # 기준 확인. 어긋나면 종료 코드 1
./local-vm.sh run run-kube-bench.sh # CIS 점검. 결과는 VM의 /var/log/kube-bench.json
./local-vm.sh down                  # VM과 디스크 삭제. 내려받은 이미지는 남는다
```

Mac의 `kubectl`로 붙으려면 kubeconfig를 꺼낸다. 관리자 권한 파일이므로 커밋하거나 공유하지 않는다.

```sh
(umask 077; mkdir -p ~/.kube; ./local-vm.sh ssh sudo cat /etc/rancher/rke2/rke2.yaml >~/.kube/bravo-onprem.yaml)
kubectl --kubeconfig ~/.kube/bravo-onprem.yaml get nodes
```

실제 머신은 `./render-user-data.sh`의 출력을 cloud-init user-data로 넣고, `verify-node.sh`와 `run-kube-bench.sh`를 노드 안에서 root로 실행한다.

설치가 10분쯤 멈췄다가 실패하면 `journalctl -u rke2-server`의 `level=fatal` 줄을 본다. CIS 조건이 맞지 않으면 RKE2가 여기에 이유를 남기고 시작을 거부한다. cloud-init 로그에는 `kubectl`이 없다는 줄만 보인다.

## 기준을 바꿀 때

RKE2와 CIS 기준은 `infra/shared/rke2/bootstrap.sh` 한 곳에 있고 T7 플랫폼 EC2도 같은 파일을 쓴다. 이 파일은 주석 한 줄만 바꿔도 T7의 user-data가 달라져 다음 `terraform apply`에서 EC2가 교체된다(`user_data_replace_on_change`). T7 담당과 맞춘 뒤 고친다.

RKE2 버전은 `render-user-data.sh`에 고정돼 있다. T7의 `rke2_version` 기본값과 출발지 EKS의 Kubernetes 1.35에 맞췄다. 올릴 때는 세 곳을 같이 본다.

## 후속 작업이 알아야 할 것

아래는 새 VM에서 잰 값이다.

- **T3 · T9 · T30:** 모든 네임스페이스에 Pod Security `restricted`가 기본으로 걸린다. 제외는 `kube-system`, `compliance-operator-system`, `tigera-operator`뿐이다. 특권 Pod는 거부된다.
- **T3:** CIS 프로필이 `default`, `kube-public`, `kube-system`에 NetworkPolicy를 넣는다. 그 밖의 네임스페이스는 직접 만든다.
- **T5:** 시크릿 암호화는 기본으로 켜져 있다(`rke2 secrets-encrypt status`가 `Enabled`).
- **T17:** `rke2-ingress-nginx`와 `rke2-traefik`을 끄므로 클러스터 안에 Ingress 컨트롤러가 없다.
- **T30:** Tailscale은 설치만 돼 있다. 로그인과 ACL은 T30에서 한다.
- **T30 · T34:** 기본 감사 정책이 `level: None`이라 `audit.log`가 0바이트다. T34의 후보 정책([#97](https://github.com/Softbank-Hackathon-Finals/platform/pull/97))이 머지되면 T30에서 적용한다.

## 남은 일

- 데모 머신이 정해지면 같은 user-data로 재설치하고 위 확인을 다시 한다(#6의 세 번째 할 일). `local-vm.sh`는 arm64 Mac 전용이고, 이 경로의 x86 설치는 아직 재지 않았다.
- `verify-node.sh`의 절전 차단은 VM 안의 Ubuntu만 확인한다. 데모 머신에 VM을 올리면 호스트의 절전 설정을 따로 끈다.
