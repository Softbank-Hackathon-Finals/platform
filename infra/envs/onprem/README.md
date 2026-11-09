# T2 온프렘 RKE2

빈 Ubuntu 24.04 VM에 user-data 하나를 넣으면 RKE2가 CIS 프로필로 뜬다. Mac의 로컬 QEMU 도구는 arm64와 x86_64 게스트를 지원하며 같은 공통 bootstrap을 사용한다. 두 아키텍처의 설치·CIS·재부팅·정리는 [추가 실측](../../../docs/evidence/t2-dual-arch-2026-11-03/README.md)에서 확인했다. 이전 arm64 측정은 [원래 증거 기록](../../../docs/evidence/t2-onprem-rke2-2026-11-03/README.md)에 보존하며, 그 기록의 WARN 분류는 추가 증빙에서 정정한다.

| 파일 | 하는 일 |
|---|---|
| [`render-user-data.sh`](render-user-data.sh) | 공통 [`bootstrap.sh`](../../shared/rke2/bootstrap.sh)에 감사 정책과 Tailscale 설치를 더해 user-data를 출력한다 |
| [`local-vm.sh`](local-vm.sh) | Mac에서 arm64 또는 x86_64 VM을 만들고, 재부팅 뒤 다시 켜고, 지운다 |
| [`local_vm.py`](local_vm.py) | 시작·종료 배타 잠금, QEMU 신원 확인, 종료 확인 후 디스크 정리 |
| [`verify-node.sh`](verify-node.sh) | 노드가 기준대로 섰는지 12개 항목을 확인한다 |
| [`run-kube-bench.sh`](run-kube-bench.sh) | kube-bench로 CIS 점검을 돌리고 요약을 출력한다 |

## 쓰는 법

로컬 VM은 `brew install qemu` 뒤 아래 순서로 쓴다. VM이 켜져 있는 동안 메모리 8GB를 차지한다.

```sh
export BRAVO_VM_ARCH=arm64         # Apple Silicon. 생략하면 실제 호스트 CPU를 선택
./local-vm.sh up                    # 새 VM 생성. cloud-init이 끝날 때까지 기다린다
./local-vm.sh run verify-node.sh    # 기준 확인. 어긋나면 종료 코드 1
./local-vm.sh run run-kube-bench.sh # CIS 점검. 결과는 VM의 /var/log/kube-bench.json
./local-vm.sh down                  # VM과 디스크 삭제. 내려받은 이미지는 남는다
```

## 재부팅 뒤 같은 VM 다시 켜기

호스트를 재시작하면 QEMU가 꺼진다. `up`은 새 디스크를 만들고 `down`은 디스크를 지우므로 이때 쓰지 않는다.

```sh
./local-vm.sh start                 # 같은 디스크·UUID·포트로 다시 켜고 SSH 접속까지 기다린다
```

- 저장된 `launch.uuid`·`connection.json`·`execution.json`을 그대로 쓴다. 실행 프로필 기록이 없는 이전 VM은 UNKNOWN으로 보고 시작하지 않는다.
- 같은 VM이 이미 실행 중이면 아무것도 하지 않고 `already running`을 출력한다. 다른 프로세스가 같은 디스크를 쓰거나 기록된 포트가 사용 중이면 시작하지 않는다.
- 디스크나 seed가 없으면 새로 만들지 않는다. 이전 부팅의 `console.log`는 `console-<UTC 시각>.log`로 보존한다.
- 시작 뒤 `up`과 같은 QEMU 신원·HVF 실행 검사를 다시 한다. VM 안의 RKE2는 스스로 다시 시작한다. OpenBao는 sealed 상태로 돌아오므로 unseal은 보호된 사람 단계로 따로 수행한다.

아키텍처를 생략하면 호스트 CPU와 HVF 하드웨어 가속을 사용한다. **다른 CPU를 지정하면 이미지 다운로드·키/디스크 생성 전에 거부한다.** 앱 이미지가 amd64뿐이라는 이유로 ARM Mac에 x86 VM을 만들지 않는다. ARM64 이미지의 발행·검증을 먼저 완료한다. Ubuntu `release-20261017` 이미지와 아키텍처별 SHA-256을 고정한다([공식 체크섬](https://cloud-images.ubuntu.com/releases/noble/release-20261017/SHA256SUMS)).

호환성 시험에서만 명시적으로 TCG를 허용한다. 다음 예외는 환경변수로 상속되지 않으며 목적과 사유가 `vm/execution.json`에 기록된다. 에뮬레이션과 호환성 시험은 성능 실측 입력으로 거부한다. Intel Mac에서 ARM64 게스트를 선택할 때도 같은 규칙이다.

```sh
BRAVO_VM_ARCH=x86_64 ./local-vm.sh up --purpose compatibility \
  --allow-emulation --reason 'isolated x86 boot compatibility test'
```

일반 생성은 기존처럼 `up`, 실제 성능 측정용 생성은 `up --purpose performance`를 쓴다. 두 경로 모두 호스트와 같은 CPU만 허용하고 가속 실패 시 TCG로 재시도하지 않는다. 내부 `_prepare`도 기록을 다시 검사해 옛 호출 방식으로 우회하지 못한다. 실행 후 PID/시작 시각·QMP UUID/디스크와 실제 QEMU CPU 종류·`-accel` 값을 대조한다. 실패하면 상태를 보존하고 실행 성공으로 기록하지 않는다.

부하·p95·복제 지연 등 성능을 비교하기 전에는 해당 VM의 `measurement` 출력을 실행 원장에 결속한다. 이 명령은 현재 프로세스를 다시 확인한다. 이전 VM에 `execution.json`이 없으면 UNKNOWN으로 거부하며, 실제 관측 없이 파일을 만들어 통과시키지 않는다. 기존 VM의 `ssh`/`down` 절차와 데이터는 그대로 보존한다.

```sh
./local-vm.sh measurement > /private/evidence/native-execution.json
```

이 결과는 실행 아키텍처에 대한 조건만 충족한다. 앱·DB·권한·관측 창 준비와 성능 기준 통과를 대신하지 않는다. 기능·복구 시험의 기존 승인은 따로 유지하며, 과거 TCG 시험의 기능 결과를 삭제하거나 성능 수치로 승격하지 않는다.

배포할 앱을 이미 정했다면, 기존 검증·전달 경로의 OCI layout과 정확한 digest도 생성 전에 검사한다. index의 platform 표기만 믿지 않고 index → manifest → config의 해시와 실제 Linux 아키텍처를 대조한다. attestation의 `unknown/unknown`은 실행 이미지로 세지 않는다. 누락·변조·ARM64 부재 시 VM 생성이 중단된다. 전체 레이어·출처·취약점 검증은 기존 `scripts/export_target_image.py` 절차를 유지한다.

```sh
./local-vm.sh up --image-layout /private/reviewed-oci \
  --image-digest sha256:<reviewed-index-digest>
```

| 게스트 | 기본 상태 경로 (`XDG_CACHE_HOME` 또는 `~/.cache` 아래) | SSH / Kubernetes API |
|---|---|---|
| arm64 (`aarch64` 별칭) | `bravo-onprem-vm` | `127.0.0.1:2222` / `127.0.0.1:6443` |
| x86_64 (`amd64` 별칭) | `bravo-onprem-vm-x86_64` | `127.0.0.1:2223` / `127.0.0.1:6444` |

같은 아키텍처를 여러 세션에서 실행할 때는 `BRAVO_VM_STATE`, `BRAVO_SSH_PORT`, `BRAVO_API_PORT`를 모두 분리한다. 사용 중인 포트는 VM 생성 전에 거부하며 QEMU도 실제 바인딩 충돌을 거부한다. 소켓 경로 길이 제한 때문에 상태 경로는 짧게 지정한다. `up`이 저장한 `vm/connection.json`의 포트를 이후 `ssh`/`run`이 사용하며, 환경변수만 바꿔 다른 포트의 VM으로 접속하지 않는다. 선택한 아키텍처와 기존 VM 메타데이터가 다르면 접속을 거부한다.

```sh
export BRAVO_VM_ARCH=x86_64
export BRAVO_VM_STATE=/tmp/bravo-x86-check
export BRAVO_SSH_PORT=52223 BRAVO_API_PORT=56444
./local-vm.sh up --purpose compatibility --allow-emulation --reason 'isolated cross-architecture boot check'
./local-vm.sh run verify-node.sh
./local-vm.sh run run-kube-bench.sh
./local-vm.sh down
```

확인된 QEMU가 실행되는 동안 `caffeinate -is -w <QEMU PID>`로 Mac의 절전을 방지한다. VM이 종료되면 이 임시 assertion도 종료되며 호스트 전역 전원 설정은 바꾸지 않는다. 공용 QEMU 설치본은 다른 세션의 VM이 사용할 수 있으므로 VM 삭제와 Homebrew 도구 제거를 별개로 판단한다.

`up`과 `down`은 같은 상태 디렉터리의 잠금을 사용한다. VM마다 새 UUID를 기록하고
QEMU에 전달하며 상태·소켓·SSH 키는 현재 사용자만 접근하도록 만든다. `down`은
양의 PID, 실행 파일·시작 시각·명령행, QMP 연결의 UUID와 열린 디스크 경로를 대조한다.
종료는 확인된 QMP 연결에 `quit`을 보내며 PID에 종료 신호를 보내지 않는다.
이 명령은 삭제할 로컬 시험 VM의 QEMU를 종료하는 것이며 게스트의 정상 종료 절차를
대신하지 않는다. 게스트 데이터를 보존해야 하면 먼저 별도 백업·종료 절차를 따른다.

QMP 응답 성공이나 연결 종료만으로 디스크를 지우지 않는다. 원래 프로세스가 사라진 것을
최대 30초간 확인한 뒤 VM 디렉터리만 삭제한다. 잘못된 PID·다른 프로세스·신원 불일치·
명령 실패·시간 초과·동시 실행은 비정상 종료하며 디스크와 로그를 보존한다.
이미 VM 디렉터리가 없으면 반복 `down`은 성공한다.
macOS의 종료 중(`E`) 또는 zombie(`Z`) 표시는 종료 완료가 아니다. 같은 시작 시각의 프로세스가 이 상태로 잠깐 보이면 제한 시간 안에서 기다리며, PID가 실제로 사라진 뒤에만 삭제한다. 다른 살아 있는 프로세스로 바뀌면 계속 상태를 보존한다.

이전 도구로 만든 VM에는 UUID/QMP가 없으므로 새 도구가 자동으로 종료·삭제하지 않는다.
이미 게스트가 종료돼 PID 파일이 없거나 오래된 상태도 보존한다. 이 경우 콘솔과 실제
QEMU 실행 파일·명령행·열린 파일을 확인해 해당 VM의 종료를 별도로 마무리하고,
프로세스와 열린 디스크 핸들이 없는 것을 확인한 후 남은 상태를 수동 정리한다.
추측한 PID를 새 파일에 넣거나 `kill`·재귀 삭제로 검사를 우회하지 않는다.
접속 메타데이터가 없는 이전 VM도 새 포트 설정으로 추측해 접속하지 않는다. 기존 절차로 필요한 자료를 보존한 뒤 종료·재생성한다. UUID/QMP가 있는 이전 VM의 `down` 안전성 검사는 그대로 사용할 수 있다.

회귀 시험은 `python3 -m unittest discover -s scripts/tests -p test_local_vm.py -v`로
실행한다. 로컬에 QEMU가 있으면 `BRAVO_TEST_QEMU=$(command -v qemu-system-aarch64)`를
같이 지정해 게스트·네트워크 없는 작은 실제 QEMU의 신원 확인·종료·정리를 추가 검증한다.
Apple Silicon에서는 32MiB의 정지된 ARM/HVF 프로세스로 실제 가속 모드 확인과 정리도 검증한다.
Ubuntu 설치·RKE2 운영 검증과 구분한다. 프로토콜은 [QEMU QMP 명세](https://www.qemu.org/docs/master/interop/qmp-spec.html)를 따른다.

Mac의 `kubectl`로 붙으려면 kubeconfig를 꺼낸다. 관리자 권한 파일이므로 커밋하거나 공유하지 않는다.

```sh
(umask 077; mkdir -p ~/.kube; ./local-vm.sh ssh sudo cat /etc/rancher/rke2/rke2.yaml >~/.kube/bravo-onprem.yaml)
kubectl --kubeconfig ~/.kube/bravo-onprem.yaml get nodes
```

위 kubeconfig의 `server` 포트는 게스트의 6443이다. x86_64 기본값이나 사용자 지정 API 포트를 사용하면 로컬 사본의 `server`를 해당 `127.0.0.1:<API 포트>`로 바꾼다.

실제 머신은 `./render-user-data.sh`의 출력을 cloud-init user-data로 넣고, `verify-node.sh`와 `run-kube-bench.sh`를 노드 안에서 root로 실행한다.

설치가 10분쯤 멈췄다가 실패하면 `journalctl -u rke2-server`의 `level=fatal` 줄을 본다. CIS 조건이 맞지 않으면 RKE2가 여기에 이유를 남기고 시작을 거부한다. cloud-init 로그에는 `kubectl`이 없다는 줄만 보인다.

`run-kube-bench.sh`는 보고서 생성용이다. 종료 코드 0은 CIS 통과를 뜻하지 않으며,
후속 CI/게이트는 JSON의 FAIL 수와 개별 WARN의 기대값·감사값을 검토해야 한다.
WARN을 모두 미실행이나 통과로 합산하지 않는다. 고정한 `rke2-cis-1.9` 결과만으로
다른 CIS 버전이나 운영 환경 전체의 준수를 선언하지 않는다.

## 기준을 바꿀 때

RKE2와 CIS 기준은 `infra/shared/rke2/bootstrap.sh` 한 곳에 있고 T7 플랫폼 EC2도 같은 파일을 쓴다. 이 파일은 주석 한 줄만 바꿔도 T7의 user-data가 달라져 다음 `terraform apply`에서 EC2가 교체된다(`user_data_replace_on_change`). T7 담당과 맞춘 뒤 고친다.

RKE2 버전은 `render-user-data.sh`에 고정돼 있다. T7의 `rke2_version` 기본값과 출발지 EKS의 Kubernetes 1.35에 맞췄다. 올릴 때는 세 곳을 같이 본다.

감사 정책 원본은 [`rke2-audit-policy.yaml`](../../../services/control-runner/templates/rke2-audit-policy.yaml) 한 곳이다. user-data는 T44 노드 묶음 [`infra/iac/nodes/<환경>`](../../iac/nodes)(기본 `target-dev`, `BRAVO_NODE_ENV`로 바꾼다)을 `manifest.json` 그대로 조립한다. 묶음은 원본과 바이트가 같고(시험 고정), 해시가 맞지 않으면 user-data를 내지 않는다. T7 EC2는 `bootstrap.sh`만 쓰므로 이 정책을 받지 않는다. 실행 중인 플랫폼 노드는 `infra/iac/nodes/apply.py`로 맞춘다([IaC 안내](../../iac/README.md)).

## 후속 작업이 알아야 할 것

아래는 새 VM에서 잰 값이다.

- **T3 · T9 · T30:** 모든 네임스페이스에 Pod Security `restricted`가 기본으로 걸린다. 제외는 `kube-system`, `compliance-operator-system`, `tigera-operator`뿐이다. 특권 Pod는 거부된다.
- **T3:** CIS 프로필이 `default`, `kube-public`, `kube-system`에 NetworkPolicy를 넣는다. 그 밖의 네임스페이스는 직접 만든다.
- **T5:** 시크릿 암호화는 기본으로 켜져 있다(`rke2 secrets-encrypt status`가 `Enabled`).
- **T17:** `rke2-ingress-nginx`와 `rke2-traefik`을 끄므로 클러스터 안에 Ingress 컨트롤러가 없다.
- **T30:** Tailscale은 설치만 돼 있다. 로그인과 ACL은 T30에서 한다.
- **T30 · T34:** 감사 로그는 `/var/lib/rancher/rke2/server/logs/audit.log`에 Metadata 수준으로 남고 Secret 값은 남지 않는다([측정](../../../docs/evidence/t30-rke2-audit-2026-11-04/README.md)). 이 정책은 새 VM부터 들어간다. 이미 떠 있는 VM은 `/etc/rancher/rke2/audit-policy.yaml`에 정책을 넣고 `rke2-server`를 재시작한다(약 42초, API 서버도 다시 뜬다).
- **T34:** 회전은 RKE2 값(100MiB, 백업 10개, 30일)이고 상한은 약 1.1GiB다. 워크로드 없는 노드에서도 하루 약 380MiB가 쌓여 노드 안 보존은 3일쯤이다. 더 오래 둘 로그는 노드 밖으로 보낸다.
- **T12 · T34:** `pods/exec` 명령 인자는 요청 주소에 실려 Metadata 수준에도 로그에 남는다. 비밀은 인자로 넘기지 않는다.

## 남은 일

- 데모 머신이 정해지면 같은 user-data로 재설치하고 위 확인을 다시 한다(#6의 세 번째 할 일). Apple Silicon 호스트에서 두 게스트를 검증해도 Intel Mac 호스트나 별도 물리 x86 머신에서의 실측으로 확대하지 않는다.
- `verify-node.sh`의 절전 차단은 VM 안의 Ubuntu를 확인한다. 이 로컬 도구의 호스트 assertion은 `pmset -g assertions`에서 별도로 확인한다.
- VM의 CPU 지원과 앱 OCI 이미지의 CPU 지원은 다르다. arm64 워크로드는 arm64 이미지가 필요하며, 현재 고정된 amd64 앱 이미지를 arm64 VM에서 검증한 것으로 처리하지 않는다.
