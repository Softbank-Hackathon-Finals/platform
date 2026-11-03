# T18 부하 생성기 EC2

측정과 데모 때만 켜는 `t3.small` 한 대를 전용 네트워크에 만든다. 인터넷에서 들어오는 경로가 없고 tailnet 안의 다른 노드가 먼저 걸어오는 연결도 거부한다. 나가는 것은 HTTPS뿐이며, 운영자는 SSM으로만 접속한다. 켜 두면 시간당 약 US$0.034, 꺼 두면 약 US$0.0025다.

**아직 만들지 않았다.** `terraform plan`과 `apply`를 실제 계정으로 돌린 적이 없다. 무엇을 쟀고 무엇을 못 쟀는지는 [검증](#검증)에 있다. 부하 생성기 자체의 사용법은 [`services/loadgen/README.md`](../../../services/loadgen/README.md)가 기준이다.

## 무엇을 왜 이렇게 만드나

| 결정 | 이유 |
|---|---|
| 전용 VPC, 피어링 없음 | 판정 입력은 평가 대상 밖에서 잰다([ADR-0010](../../../docs/adr/0010-observability-boundary.md)). 플랫폼 노드는 CPU 예약이 거의 찼다([capacity](../platform-aws/capacity.md)) |
| 공인 IP 자동 할당, 인바운드 규칙 0 | 나가는 길에만 쓴다. NAT 게이트웨이(시간당 $0.059)보다 싸다. 같은 구성을 [`ci-validation-aws`](../ci-validation-aws)가 쓴다 |
| 바깥 방향 TCP 443만 | Cloudflare Edge, SSM, apt, PyPI, GitHub, Tailscale 제어 · 중계가 모두 443이다 |
| CPU 크레딧 `unlimited` | T3는 시작 크레딧이 없어(AWS 문서) `standard`면 켜자마자 vCPU당 20%로 묶인다. 부하 생성기가 느려지면 대상이 느린 것처럼 보인다. 다른 스택이 `standard`인 것과 일부러 다르다 |
| IAM은 SSM 코어 + 비밀 읽기 명시적 거부 | SSM 코어 정책에는 Parameter Store 읽기가 들어 있어 [`platform-aws`](../platform-aws)처럼 막는다 |
| `component=platform`, `purpose=load-generator` | [`cost_report.py`](../../../scripts/cost_report.py)는 `source` · `platform` · `target`만 귀속한다. `loadgen` 같은 값은 미귀속으로 빠진다 |
| 자동 종료 타이머 없음 | 켜 둔 채 잊어도 하루 약 $0.80이다. 데모 중에 꺼지는 쪽이 훨씬 비싸다 |

인스턴스를 끈 상태에서 `terraform apply`를 해도 교체되지 않게 막았다. 끄면 자동 할당 공인 IP가 풀리는데, 공급자(v6.71.0)는 이를 교체가 필요한 변경으로 읽기 때문이다. 소스를 읽고 `ignore_changes`를 둔 것이고 실제 AWS에서 돌려 보지는 않았다.

## 사양 하한: 2 vCPU · 2 GiB · x86_64

기준값은 [T18 증거](../../../docs/evidence/t18-load-generator-2026-11-04/README.md) 3절이다. **Apple M5에서 잰 값이라 EC2로 그대로 옮길 수 없다.** 하한은 판단이고, 실제 머신 확인은 [첫 실행 확인](#첫-실행-확인)에서 한다.

| 항목 | 잰 값 (M5) | 하한을 정한 이유 |
|---|---|---|
| CPU | 평균 201~270m, 1초 최대 401m. 코어 1개를 넘지 않는 프로세스 | T3 vCPU는 코어의 스레드 하나다. 부하 프로세스와 `tailscaled` · OS가 서로 밀어내지 않게 2개로 둔다 |
| 메모리 | 부하 약 65 MiB, 3분 실행의 대조까지 114.5 MiB | 대조는 기록 전체를 메모리에 올린다. 단순 비례로 시도 1건당 약 1.4 KiB이므로 1시간 실행의 대조는 약 1 GiB다(추정, 재지 않음). 1 GiB 사양은 OS와 함께 못 담는다. 더 길게 돌리면 사양을 올리거나 `reconcile`을 다른 곳에서 돌린다 |

## 입력

[`operator.tfvars.example`](operator.tfvars.example) 형식의 파일을 저장소 밖에 둔다. 계정 ID, AMI, 태그를 받는다. 비밀 값을 받는 입력은 없다.

AMI는 Canonical 공개 SSM 파라미터 `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id`에서 읽고 소유자와 `x86_64`를 대조한다. 아키텍처 검사는 인스턴스 종류만 본다. 다른 아키텍처의 AMI를 넣으면 plan은 지나가고 apply에서 AWS가 거부한다.

## 배포와 삭제

상태 파일은 기존 암호화 S3 백엔드에 별도 키로 둔다. 입력 · 상태 · plan 파일과 인스턴스 ID는 Git에 넣지 않는다.

```sh
terraform init -backend-config=/private/path/loadgen.backend.json
terraform plan -var-file=/private/path/loadgen.tfvars -out=/private/path/loadgen.tfplan
terraform apply /private/path/loadgen.tfplan
```

태그, AMI, 부팅 스크립트를 바꾸면 인스턴스가 교체되고 디스크도 새로 만들어진다. 측정 결과를 먼저 옮긴다. SSM 파라미터의 AMI는 새 판이 나오면 값이 바뀌므로, 다시 읽은 값을 그대로 넣지 않는다.

다 쓰면 이 상태의 자원만 지운다. 디스크의 측정 결과도 함께 지워지므로 먼저 옮긴다.

```sh
terraform plan -destroy -var-file=/private/path/loadgen.tfvars -out=/private/path/loadgen-destroy.tfplan
terraform apply /private/path/loadgen-destroy.tfplan
```

## 측정 때만 켜기

운영자 PC에서 켜고 접속한다. Session Manager 플러그인이 필요하다.

```sh
INSTANCE_ID=$(terraform output -json operator | jq -r .instance_id)
aws ec2 start-instances --instance-ids "$INSTANCE_ID"
aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID"
aws ssm start-session --target "$INSTANCE_ID"
```

접속한 뒤 `tmux` 안에서 실행한다. Session Manager는 입력이 없으면 기본 20분 뒤 세션을 끊는다. `read -rs`는 입력을 화면에 보이지 않게 받는 명령이고, 값을 붙여 넣은 뒤 Enter를 누른다.

```sh
cloud-init status --wait && test -f /var/lib/bravo-loadgen-ready   # 첫 부팅 설치 완료 확인
tmux new -A -s loadgen bash
read -rs TS_KEY && printf '%s' "$TS_KEY" | sudo bravo-tailnet-join; unset TS_KEY
cd ~ && git clone https://github.com/Softbank-Hackathon-Finals/platform.git   # #158 머지 전에는 --branch로 받는다
cd platform/services/loadgen && uv sync
read -rs LOADGEN_DB_DSN && export LOADGEN_DB_DSN      # DB 경로가 정해진 뒤
read -rs LOADGEN_LOCK_DSN && export LOADGEN_LOCK_DSN  # DB 경로가 정해진 뒤
uv run bravo-loadgen run \
  --base-url https://<Edge 주소> --measurement-path edge \
  --plan-id plan-demo --source-env aws --target-env onprem \
  --duration 300 --out out/run1 \
  --db-dsn-env LOADGEN_DB_DSN --lock-dsn-env LOADGEN_LOCK_DSN \
  --window-url https://<플랫폼 수신 주소>
```

DB 경로와 수신 주소가 정해지기 전에는 해당 줄과 옵션을 뺀다. 그때 리포트는 `NOT_RECONCILED`이고 종료 코드는 2다. 통과가 아니므로 나중에 `reconcile`로 대조한다.

**결과 가져오기.** 이 호스트에는 S3 권한도 들어오는 포트도 없으므로 SSM 포트 포워딩으로 받는다. 호스트에서 `python3 -m http.server 8080 --bind 127.0.0.1 --directory out/run1`을 띄우고 운영자 PC에서 아래를 실행한다.

```sh
aws ssm start-session --target "$INSTANCE_ID" --document-name AWS-StartPortForwardingSession --parameters portNumber=8080,localPortNumber=18080
for f in journal.jsonl run-state.json windows.jsonl report.json report.md; do curl -fO "http://127.0.0.1:18080/$f"; done
```

**다섯 파일을 모두 받는다.** `reconcile`은 `journal.jsonl` · `run-state.json` · `windows.jsonl`을 읽는다. `report.json`만 받고 인스턴스를 지우면 나중에 대조할 수 없다. `journal.jsonl`은 저장소 밖에 두고, 공개 기준은 [`services/loadgen`](../../../services/loadgen/README.md)의 산출물 안내를 따른다.

**끄기.** 메모리의 비밀 값은 사라지고 디스크의 결과는 남는다.

```sh
aws ec2 stop-instances --instance-ids "$INSTANCE_ID"
```

### 첫 실행 확인

아래는 실제 머신에서만 알 수 있다. 첫 측정을 판정 근거로 쓰기 전에 본다.

| 확인 | 기대 | 어긋나면 |
|---|---|---|
| `tailscale status` | 이 노드와 수신 대상이 보이고 경로가 `relay`로 표시됨 | ACL의 태그 규칙과 키의 태그를 확인한다 |
| 리포트 경고 `GENERATOR_LAGGING` | 없음 | 부하 생성기가 병목이다. `instance_type`을 올린다 |
| 리포트 경고 `WINDOW_DELIVERY_FAILED` | 없음 | Tailscale 중계 경로가 창 전송 제한 2초를 못 맞춘 것일 수 있다. 아래 표의 UDP 항목을 본다 |
| 리포트 경고 `LOCK_PROBE_STALLED` | 없음 | 중계 경로 지연인지 DB 멈춤인지 가른다 |
| `ulimit -Hn` | 5,506 이상 (`50×60 + 150×15 + 256`) | 부하 생성기가 시작하지 않고 종료 코드 2로 끝난다 |
| `timedatectl` | 시계 동기화 `yes` | 창 시각을 믿을 수 없다. UDP 123이 막혀 있어 공용 NTP로는 맞출 수 없으므로 시간 서버를 [Amazon Time Sync](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-ec2-ntp.html)의 `169.254.169.123`으로 설정한다. [보안 그룹이 거르지 않는 주소](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-security-groups.html)다 |
| `uv run python -V` | 3.12 (증거는 3.14.7) | 값이 크게 다르면 파이썬 판 차이부터 의심한다 |
| CloudWatch `CPUSurplusCreditsCharged` | 비용 표의 범위 안 | 부하 시간 또는 사양을 다시 본다 |

## 비밀 값 전달

**Tailscale 키와 DB 연결 문자열은 운영자의 SSM 세션에서 프로세스 환경으로만 들어간다.** Terraform 입력 · 상태 · user-data에 없고, 호스트 역할은 비밀을 읽을 수 없다.

| 기존 방식 | 위치 | 여기서 |
|---|---|---|
| 환경변수 **이름**만 받는다 | `services/loadgen` CLI | 그대로 쓴다 |
| 호스트 역할의 비밀 읽기 명시적 거부 | `platform-aws`, `ci-validation-aws` | 그대로 쓴다 |
| Terraform은 ARN만 받고 그 ARN만 읽게 허용 | `subscription-runtime` | 쓰지 않는다. 이번 범위의 IAM은 SSM 최소 권한이다 |
| ESO로 Pod에 전달 | `target-secrets`, `source-workloads` | 쓰지 않는다. 클러스터 밖 호스트다 |

Tailscale 키는 [태그 · 1회용 · 임시(ephemeral)](https://tailscale.com/kb/1085/auth-keys)로 만들고 수명을 가장 짧게 둔다. `bravo-tailnet-join`은 키를 [표준 입력에서 읽어](https://tailscale.com/kb/1241/tailscale-up) 명령줄에 남기지 않고, 이 호스트로 들어오는 tailnet 연결을 [`--shields-up`](https://tailscale.com/kb/1072/client-preferences)으로 거부한다. 터널 안쪽은 보안 그룹이 보지 못하고 ACL은 이 스택 밖에 있기 때문이다. `AWS-RunShellScript`로는 비밀을 넘기지 않는다. 명령 내용이 SSM 기록에 남는다.

## 정해야 할 것

코드에 고정하지 않았다. 정해지기 전에도 부하와 `windows.jsonl`은 돌아간다.

| 항목 | 지금 | 정할 일 |
|---|---|---|
| Tailscale 태그와 ACL | `tailscale_tag` 입력. `tag:bravo-*`만 받는다 | tailnet 소유자(T30)가 태그 소유자와, 이 태그에서 플랫폼 수신 포트로만 가는 규칙을 추가한다. ACL은 이 스택 밖이다 |
| Tailscale 키 발급자 | 미정 | 측정할 때마다 키를 만들 사람 |
| DB 읽기 경로와 계정 | 미정. 이 스택은 DB 자원을 만들지 않는다 | `messages` SELECT 계정, 잠금 측정용 직접 연결, tailnet 경로. T30은 5432를 복제 역할만 허용하므로 그 규칙과 함께 검토한다. 그전에는 부하만 돌리고 [`reconcile`을 DB에 닿는 곳에서](../../../services/loadgen/README.md#실행-위치와-창-전송) 돌린다 |
| 창 수신 주소 | 없음 (T10 · T17) | `--window-url`에 넣을 주소 |
| Tailscale UDP | 열지 않음. [UDP가 막히면 중계(DERP)로만 통신한다](https://tailscale.com/kb/1082/firewall-ports) | 첫 실행에서 느리면 바깥 방향 UDP 추가를 검토한다. 시험이 막으므로 리뷰가 필요한 변경이다 |

## 비용

서울 리전, 2026-11-04에 공개 자료로 읽은 값이다. 실제 청구서가 아니다.

| 상태 | 항목 | 시간당 | 하루(24시간) |
|---|---|---:|---:|
| 꺼짐 | EBS gp3 20 GiB ($0.0912/GiB·월 ÷ 730) | $0.0025 | $0.06 |
| 켜짐 | 위 + `t3.small` $0.026 + 공인 IPv4 $0.005 | $0.0335 | $0.80 |
| 부하 중 추가 | 인터넷 송신 약 0.3 GB × $0.126 | 약 $0.04 | 24시간 부하 시 약 $0.91 |
| 부하 중 추가 | CPU 초과분. vCPU 1개 포화 시 $0.03, 2개 포화 시 $0.08 ($0.05/vCPU·시 × (사용 vCPU − 기준 0.4)) | 최대 $0.08 | 최대 $1.92 |

- **5일 예시:** 켜짐 20시간, 그중 부하 10시간(최악의 CPU 초과분 포함), 나머지 꺼짐이면 약 $2.1이다. 5일 내내 켜 두되 부하가 없으면 약 $4.0이다.
- **송신량은 추정이다.** 코드의 요청 모양으로 쓰기 1건 약 550바이트, 읽기 약 380바이트를 잡아 초당 약 85 KB로 계산했다. 들어오는 응답은 AWS가 과금하지 않는다. 계정 전체 월 100 GB 무료 구간은 빼지 않았다.
- **CPU 초과분은 끌 때 정산된다.** 인스턴스를 멈추면 남은 초과 크레딧이 바로 청구된다.
- **태그 귀속:** 자동 할당 공인 IP 요금이 `component`로 귀속되는지는 확인하지 못했다. `project_unattributed`로 잡힐 수 있다.
- **예산:** 이 금액은 작지만 플랫폼 스택 합계가 이미 예산을 넘는다([계산](../platform-aws/README.md#retained-runtime-cost-baseline)). 쓰지 않을 때는 끈다.

출처: t3.small [DoiT](https://www.doit.com/compute/spot/ap-northeast-2/t3.small) · [aws-pricing.com](https://aws-pricing.com/ap-northeast-2.html), gp3 [aws-pricing.com](https://aws-pricing.com/ap-northeast-2.html), 공인 IPv4 [AWS VPC 요금](https://aws.amazon.com/vpc/pricing/), CPU 초과분 [AWS Unlimited 문서](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-unlimited-mode-concepts.html)(us-east-1 표의 값), 시작 크레딧 없음 [AWS 크레딧 문서](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-credits-baseline-concepts.html), 인터넷 송신 [CostGoat](https://costgoat.com/pricing/aws-data-transfer)(AWS Price List를 읽었다고 표시, 2026-10-20 갱신).

t3.small과 gp3, 인터넷 송신은 제3자 집계이고 AWS 원본을 직접 읽지 않았다. t3.small은 [`platform-aws`](../platform-aws/README.md#retained-runtime-cost-baseline)에 기록된 Price List의 t3.large $0.104의 정확히 1/4이다. apply 전에 AWS 자격 증명으로 확인한다.

```sh
aws pricing get-products --region us-east-1 --service-code AmazonEC2 --filters Type=TERM_MATCH,Field=regionCode,Value=ap-northeast-2 Type=TERM_MATCH,Field=instanceType,Value=t3.small Type=TERM_MATCH,Field=operatingSystem,Value=Linux Type=TERM_MATCH,Field=tenancy,Value=Shared Type=TERM_MATCH,Field=preInstalledSw,Value=NA Type=TERM_MATCH,Field=capacitystatus,Value=Used
```

## 검증

**돌린 것** (2026-11-04, AWS 호출과 자격 증명 없음)

| 검사 | 결과 |
|---|---|
| Terraform 1.16.5 `fmt -check`, `init -backend=false`, `validate` | 통과 |
| `terraform test` (모의 공급자) | 20건 통과. 양성 8건 + 거부 12건 |
| 일부러 망가뜨리기 | 복사본에 결함 61종을 하나씩 넣어 61종 모두 시험이 잡음. 둘째 보안 그룹과 추가 IAM 정책은 독립 리뷰가 찾아 시험에 더한 것이다. 스크립트는 저장소에 넣지 않아 다시 돌릴 수 없다 |
| Trivy `config` HIGH · CRITICAL (`.trivyignore.yaml` 적용, CI와 같은 명령) | 통과. 예외 없이 돌리면 바깥 방향 HTTPS(AWS-0104)가 나온다 |
| 부팅 스크립트 | amd64 Ubuntu 24.04 컨테이너에서 iptables로 바깥 방향을 TCP 443과 DNS만 열고 실행. 80번 차단을 먼저 확인했고, 설치와 비루트 사용자의 `uv sync`, `bravo-loadgen run --help`까지 통과. Ubuntu 기본 `sudo`(`use_pty`)의 파이프로 `/dev/stdin`을 읽는 것과 `bravo-tailnet-join`의 플래그 해석도 확인했다. 로그인 전 단계에서 멈췄고 키가 출력되지 않았다 |
| 공급자 동작 | v6.71.0 소스에서 정지 시 공인 IP 교체 계획과, 고정 성능 종류의 크레딧 설정 처리를 읽음 |

거부 시험은 인바운드 규칙, HTTPS 외 아웃바운드, 사양 미달(메모리 · vCPU), 다른 아키텍처, 태그 누락 · 계약 외 값, 잘못된 Tailscale 태그 · 계정 · AMI를 다룬다. 스택의 자원 종류와 개수도 검토한 목록과 정확히 같아야 한다. 자원을 하나 더 넣으면 시험이 실패하므로 그때 경계와 태그 시험을 함께 고친다.

**모의 시험의 한계.** 모의 공급자는 `default_tags`를 합치지 않아, 자원마다 태그를 직접 달아 시험으로 잰다. `default_tags`가 실제로 합쳐지는지는 재지 않았다. 인바운드 · 아웃바운드 사전 조건은 AWS가 돌려준 규칙을 재정의로 흉내 낸 것이다.

**재지 못한 것**

- 실제 계정의 plan · apply · destroy, 실제 AMI의 부팅, SSM 접속과 포트 포워딩, `unlimited` 적용, 보안 그룹 실제 동작
- 콘솔에서 인바운드 규칙을 추가했을 때 다음 plan이 실제로 멈추는지
- 정지 · 시작 뒤 plan에 변경이 없는지. 소스를 읽어 막았을 뿐 돌려 보지 않았다
- `t3.small`에서 쓰기 50 · 읽기 150 req/s의 CPU · 메모리, `GENERATOR_LAGGING` 여부, 응답 없는 대상에 연결이 최대 5,506개까지 쌓일 때의 메모리
- 실제 Edge 경유 측정과 대상 쪽 결과. 이 작업은 부하를 한 건도 보내지 않았다
- Tailscale 실제 로그인(`--auth-key=file:/dev/stdin`), `--shields-up`의 실제 차단, 임시 노드의 정지 뒤 재가입, 중계 경로의 지연과 창 전송 2초 제한
- 실제 이미지의 시간 동기화
- 실제 청구 금액과 태그 귀속. 컨테이너 시험은 EC2 이미지가 아니며 VPC 해석기와 시간 동기화 경로는 흉내 내지 않았다
