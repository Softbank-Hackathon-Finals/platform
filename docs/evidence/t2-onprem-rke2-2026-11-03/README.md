# T2 온프렘 RKE2 검증 — 2026-11-03

새 Ubuntu 24.04 VM에 [`render-user-data.sh`](../../../infra/envs/onprem/render-user-data.sh)의 출력을 user-data로 넣어 RKE2 `v1.35.9+rke2r1`을 CIS 프로필로 세우고 결과를 쟀다. 측정 환경은 Apple Silicon Mac의 QEMU VM(arm64, 4 vCPU, 메모리 8GB)이다. **온프렘 데모 머신은 아직 정해지지 않았으므로 이 기록은 데모 머신 검증이 아니다.** 소스 파일 해시와 항목별 값은 [`results.json`](results.json)에 있다.

| 검증 | 결과 | 증거 수준 |
|---|---|---|
| 새 VM 설치 | cloud-init 정상 종료, 104초 | 로컬 VM 실측 |
| 노드 기준 `verify-node.sh` | 10개 통과 | 로컬 VM 실측 |
| kube-bench 0.16.0 `rke2-cis-1.9` | pass 82 · fail 0 · warn 41 · info 7 | 로컬 VM 실측 |
| Mac에서 `kubectl` 접속 | 노드 1대 Ready, Pod 17개 전부 Running 또는 Completed | 로컬 VM 실측 |
| sysctl · etcd 조건을 뺀 user-data | RKE2 시작 거부, cloud-init 실패, `verify-node.sh` FAIL 7 · PASS 3 | 로컬 VM 실측 |
| 실행 중 노드에서 etcd 사용자 · 그룹 삭제 후 재시작 | `systemctl start` 종료 코드 1, `missing required: user: unknown user etcd` | 로컬 VM 실측 |
| API 서버 매니페스트 권한을 666으로 변경 | kube-bench fail 1(1.1.1), 600으로 되돌리자 fail 0 | 로컬 VM 실측 |
| shellcheck, bash 문법, 공개 파일 검사 | 통과 | 로컬 |

조건 삭제, etcd 삭제, 권한 변경 세 줄은 설치 스크립트와 검증 도구가 실제로 어긋남을 잡는지 확인한 음성 시험이다.

## kube-bench 읽는 법

`rke2-cis-1.9`는 kube-bench 0.16.0에 있는 RKE2 기준 중 가장 새것이고, Kubernetes 1.35보다 오래된 기준이다. 그래서 점수 하나로 요약하지 않고 항목별로 읽는다. WARN 41개는 모두 kube-bench가 자동으로 판정하지 않는 Manual 항목이다. 이 결과는 CIS 기준 전체 충족을 뜻하지 않는다.

## 조건이 빠졌을 때 걸리는 시간

조건을 뺀 user-data는 621초 뒤에 실패로 끝났다. 공통 bootstrap의 `systemctl enable --now`는 시작이 실패해도 종료 코드 0을 돌려준다(같은 VM에서 `start` 1, `enable --now` 0). 이어서 API 서버를 120회, 10분 기다린 뒤 `kubectl` 파일이 없어 멈춘다. 실패를 놓치지는 않지만 원인은 cloud-init 로그가 아니라 `journalctl -u rke2-server`의 `level=fatal` 줄에 남는다. T7 EC2가 교체되지 않도록 이번 작업에서 이 파일은 바꾸지 않았다.

## 새 VM에서 본 값

- 시크릿 암호화 `Enabled`
- 감사 정책 `level: None`, `audit.log` 0바이트
- Pod Security `restricted` 기본 적용. 제외는 `kube-system`, `compliance-operator-system`, `tigera-operator`
- 설치 직후 메모리 사용 약 1.2GB, 디스크 사용 5.2GB. 이때 Pod 3개는 아직 생성 중이었으므로 안정 상태의 값은 아니다

설치 시간은 환경에 따라 달라진다. 같은 Mac에서 `v1.36.5`를 시험했을 때는 424초와 613초가 걸렸고, 차이의 원인은 따로 확인하지 않았다.

## 검증하지 않은 것

데모 머신 재설치(#6의 세 번째 할 일), 이 경로의 x86 설치, Tailscale 로그인과 연결, 재부팅 후 복구, 호스트 절전, CNPG · OpenBao · 앱 같은 워크로드, T7 EC2의 kube-bench는 재지 않았다.
