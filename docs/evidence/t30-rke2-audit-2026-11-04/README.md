# T30 RKE2 감사 정책 적용 검증 — 2026-11-04

온프렘 user-data가 RKE2 첫 시작 전에 [`rke2-audit-policy.yaml`](../../../services/control-runner/templates/rke2-audit-policy.yaml)을 넣게 바꾸고, 실제로 기록되는지 쟀다. 측정 환경은 Apple Silicon Mac의 새 QEMU VM(arm64, 4 vCPU, 메모리 8GB)이다. **로컬 VM 결과다. 온프렘 데모 머신, T7 EC2, 로그 전달, 중앙 검색은 검증하지 않았다.** 소스 파일 해시와 항목별 값은 [`results.json`](results.json)에 있다.

| 검증 | 결과 | 증거 수준 |
|---|---|---|
| 새 VM 설치 | cloud-init 정상 종료, 117초 | 로컬 VM 실측 |
| 노드 기준 `verify-node.sh` | 12개 통과(감사 항목 2개 추가) | 로컬 VM 실측 |
| RoleBinding 생성 · 삭제 | `audit.log`에 Metadata · ResponseComplete로 201, 200. 본문 필드 없음 | 로컬 VM 실측 |
| Secret 생성 · 조회 · 수정 · 삭제 | 값과 base64 값 0건. 대조군으로 이름은 6건 남았고 API는 값을 돌려줬다 | 로컬 VM 실측 |
| ConfigMap 값, 새로 발급한 토큰 | 로그에 0건 | 로컬 VM 실측 |
| T34 AUD-01.3 판정기에 실제 이벤트 입력 | 생성 · 삭제 모두 있으면 `present`. 삭제 없으면 마감 전 `NOT_RUN`, 마감 뒤 `absent`. 다른 주체면 `absent` | 로컬 사본 입력. 전달 · 검색은 안 거침 |
| 로그 회전 강제 | 백업 10개 + 현재 파일, 합계 1,061MiB. 회전 뒤에도 12개 통과 | 로컬 VM 실측 |

## 음성 시험

| 바꾼 것 | 결과 |
|---|---|
| user-data에서 정책 부분만 삭제 | `verify-node.sh` 종료 코드 1. 감사 2개만 FAIL, 나머지 10개 PASS. `audit.log` 0바이트 |
| 그 VM에 정책을 넣고 `rke2-server` 재시작 | 42초, API 서버 재시작, 12개 통과 |
| Secret 본문을 남기는 틀린 정책으로 교체 | `Secret 값 없음`만 FAIL. 요청 본문이 든 Secret 이벤트 1건을 로그에서 확인 |
| 다시 원래 정책으로 교체 | 12개 통과 |

정책 파일이 있는지는 판정에 쓰지 않았다. 정책을 넣지 않아도 RKE2가 같은 경로에 `level: None` 파일을 만들고, API 서버 인자도 똑같기 때문이다. 그래서 `verify-node.sh`는 서버 측 dry-run(실제로 저장하지 않는 요청)을 보내고 로그에 남았는지를 본다.

## kube-bench는 이 차이를 구별하지 못한다

정책이 있을 때와 없을 때 모두 pass 82 · fail 0 · warn 41 · info 7이고, 상태가 달라진 항목은 없다. 3.2.1(최소 감사 정책)은 `level: None`이어도 PASS이고, 3.2.2(정책 내용)는 양쪽 다 수동 항목이라 WARN이다. kube-bench 결과를 감사 기록의 증거로 쓰지 않는다.

## 정책 검토

고쳐야 할 결함은 찾지 못했다. 규칙은 머리 주석만 바꿨다(자동 적용이 아니라는 문구가 낡았다).

- Secret 본문: `Request`와 `RequestResponse` 규칙이 하나도 없어 본문이 기록될 길이 없다. 위 실측과 일치한다.
- 권한 변경: RBAC 4종을 명시했고, 그 밖의 요청도 마지막 규칙에서 Metadata로 남는다.

주의할 점 둘을 실측으로 확인했다.

- **`pods/exec` 명령 인자는 남는다.** 인자가 요청 주소(`requestURI`)에 실려 Metadata 수준에도 기록된다. 정책으로 가릴 수 없으므로 비밀은 인자로 넘기지 않는다. 표식 문자열로만 시험했다.
- **거부된 생성은 이름으로 찾을 수 없다.** 403 이벤트는 남지만 본문을 읽기 전에 끝나 `objectRef.name`이 없다. T34 판정은 성공 이벤트만 쓰므로 영향이 없다.

## 회전과 보존

회전 값은 이번 변경이 정한 것이 아니라 RKE2 CIS 프로필이 넣은 값이다. 파일당 100MiB, 백업 10개, 30일, 압축 없음이다. 별도 디스크 한도는 없고 루트 디스크를 함께 쓴다. 상한은 약 1.1GiB로, 40GB 디스크의 3% 정도다.

**30일은 실제 보존 기간이 아니다.** 워크로드가 없는 노드에서 5분 동안 1,361,149바이트, 하루로 환산해 374MiB가 쌓였다(앞선 측정 384MiB). 이 속도면 약 2.9일 만에 가장 오래된 파일부터 지워진다는 계산이고, 워크로드를 올리면 더 짧아진다. 기록량의 약 75%는 리더 선출 리스(lease) 갱신이다. 3일 넘게 둘 로그는 노드 밖으로 보내야 한다.

## 재현

```sh
cd infra/envs/onprem
./local-vm.sh up
./local-vm.sh run verify-node.sh
./render-user-data.sh | sed '/^install -d -m 700/,/^AUDIT_POLICY$/d' >/tmp/no-policy.user-data
USER_DATA=/tmp/no-policy.user-data ./local-vm.sh up   # 앞 VM을 down한 뒤
```

회전은 VM 안에서 관리자 인증서로 `/version?pad=<16KiB>` 요청을 83,435회 보내 일으켰다. 원본 이벤트와 측정 스크립트는 공개하지 않고, 공개한 값은 이벤트 해시와 정규화한 결과다.

## 측정 중 드러난 것

`local-vm.sh down`이 세 번 모두 `QEMU exit not confirmed`로 멈춰 VM 폴더가 남았다. 프로세스와 열린 파일이 없어진 것을 확인한 뒤 README의 수동 절차로 지웠다. [#119](https://github.com/Softbank-Hackathon-Finals/platform/pull/119)가 고치는 증상과 같아 보여 여기서는 고치지 않았다. #119 코드로 다시 재지는 않았다.

## 검증하지 않은 것

데모 머신 설치, 이 경로의 x86 설치, T7 EC2와 T6 EKS의 감사 로그, CNPG `log_connections`와 pgAudit, 로그 전달 · 중앙 검색 · 검색 권한 · 10분 창, 워크로드가 있을 때의 로그량, 디스크가 찼을 때의 동작, 형식이 틀린 정책 파일에서의 동작, 재부팅 후 복구는 재지 않았다.
