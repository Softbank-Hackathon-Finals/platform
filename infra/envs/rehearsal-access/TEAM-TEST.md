# 개인 인증으로 직접 API 시험

`team_test.py`는 이미 권한을 받은 팀원의 Tailscale 신원으로 시험을 실행한다.
운영자가 제공한 비공개 `team-test.json`에는 개인 인증 API와 직접 API 주소, 클러스터 UID,
관리 namespace와 개인 로그인명 → ServiceAccount 매핑만 넣는다.
[예시](team-test.example.json)는 실제 접속 설정이 아니다.
이 도구는 계정을 등록하거나 RBAC를 부여하지 않는다.

Python 3.11 이상과 kubectl이 설치된 macOS, Linux, WSL에서 사용한다.
Tailscale에 본인 계정으로 연결한 뒤 실행한다. 기본 프로필 위치는 스크립트 옆의
`team-test.json`이다. 배포 ZIP의 `bravo-team-test.py`도 같은 스크립트다.

```sh
python3 team_test.py --profile /private/team-test.json -- kubectl auth whoami
python3 team_test.py --profile /private/team-test.json -- \
  kubectl --as=system:serviceaccount:YOUR_NAMESPACE:YOUR_ACCOUNT auth whoami
python3 team_test.py --profile /private/team-test.json \
  --diagnostics /private/new-test-diagnostics.json -- python3 YOUR_TEST.py
```

시험 명령은 래퍼가 설정한 `KUBECONFIG`를 사용해야 한다. 별도 kubeconfig로
덮어쓰면 이 연결을 사용하지 않는다. 일반 Tailscale 인증 프록시는 `--as`를
개인 신원으로 바꾸므로 권한 거부 시험에 직접 사용하지 않는다.

## exec 뒤 연결이 끊기던 문제

이전 도구는 API Pod로 연 포트포워드 안에서 `kubectl exec`를 실행했다.
exec가 정상 종료되어도 외부 포트포워드가 `broken pipe` / `lost connection to pod`로
종료되면 다음 API 요청이 연결 거부를 받았다. WSL kubectl 1.35에서는 SPDY를
강제해도 반복 실행 뒤 같은 문제가 보고됐다.

현재 도구는 **포트포워드를 만들지 않는다.** `server`의 기존 개인 인증 프록시에서
본인 ServiceAccount의 10분 토큰을 발급받은 뒤, `direct_server`의 별도 noauth API
프록시로 시험 요청을 보낸다. 이 프록시는 bearer와 impersonation 헤더를 보존한다.
Kubernetes API가 인증과 RBAC를 판단하며, noauth는 Kubernetes 인증 해제가 아니다.
WebSocket/SPDY 선택을 강제하지 않으며 끊긴 시험 명령을 자동 재실행하지 않는다.
[Tailscale noauth 계약](https://tailscale.com/docs/kubernetes-operator/api-server-access/noauth-mode)을 따른다.

## 설치자: 직접 경로 준비

사람이 사용하는 tailnet에 기존 auth 프록시와 **별도의** `ProxyGroup`을 만들고
`spec.type: kube-apiserver`, `spec.kubeAPIServer.mode: noauth`를 사용한다.
[설치 예시](team-api.example.yaml)의 이름·태그·이미지 ProxyClass를 현재 환경으로
대조한다. 기존 auth 프록시의 모드를 바꾸면 개인 신원으로 토큰을 발급할 수 없다.
워크로드 전용 tailnet의 noauth 주소를 사람의 장치가 접근할 수 있다고 가정하지 않는다.

기존 허용 대상의 TCP 443 정책을 재사용하며 RBAC·사용자 범위를 넓히지 않는다.
Tailscale Services에서 해당 프록시 호스트를 승인한 뒤 서비스 광고, ProxyGroup의
Ready, HTTPS 인증서와 실제 API 연결을 확인한다. Pod Running만으로 준비를 판정하지
않는다. `direct_server`에는 ProxyGroup status.url의 전체 FQDN을 넣는다.
운영 프로필·사용자 매핑·실제 endpoint는 공개 저장소에 커밋하지 않는다.

## 수명과 정리

- 먼저 개인 신원과 `kube-system` UID를 확인한다. 본인에게 매핑된 ServiceAccount의
  10분 TokenRequest를 발급하고 직접 API에서도 서비스계정 신원과 같은 UID를 확인한다.
  auth 프록시가 대신 응답하거나 다른 클러스터이면 시험 명령을 시작하지 않는다.
- 토큰은 프로세스 메모리와 자식 환경에만 둔다. kubeconfig에는 ExecCredential
  호출만 기록한다. 관리자 개인키와 실제 토큰을 파일이나 Slack으로 나누지 않는다.
- 자격 만료·Ctrl-C·SIGTERM에는 자기 명령과 자식 프로세스를 종료한다. 시험을
  자동 재실행하지 않는다. 종료 시 임시 설정을 제거한다.
- `--diagnostics`에는 전송 방식·검증한 주체·만료·명령 종료 코드만 새 비공개 파일에
  기록한다. 시험 stdout이나 토큰은 담지 않는다. 연결 실패는 명령의 실패로 처리한다.
- 시험이 만든 Kubernetes 자원은 시험 코드가 UID 전제조건으로 정리해야 한다.
  연결 유실 시 원 실행의 자원과 결과를 먼저 읽고 정리한 뒤 명시적으로 재시도한다.
- 명령에 환경변수나 `kubectl config view --raw`를 출력하도록 지시하면 자격을 노출할
  수 있다. 이 래퍼는 명시적으로 실행하는 신뢰된 시험에 사용한다.

## T15 자격 이전 시험을 실행할 때

공식 `services/rehearsal-runner/testing/rke2_credential_flow.py`와 해당 담당이
지정한 소스 SHA·후보 digest·설정으로 실행한다. `services/platform-events`에서:

```sh
export PYTHONPATH=../rehearsal-runner/src:../agent-worker/src
python3 /path/to/team_test.py --profile /private/team-test.json \
  --diagnostics /private/new-t15-diagnostics.json -- \
  uv run --frozen --no-default-groups \
    --with pydantic --with pyjwt --with cryptography \
    python ../rehearsal-runner/testing/rke2_credential_flow.py run \
    --config /private/config.json
```

실행 전에 같은 명령의 `diff` 결과를 후보 이미지의 원 diff와 대조한다.
`isolation_image`는 목적지 CPU를 지원하는 digest여야 한다. `bao_image`는
writer/probe Job용이며, 후보 앱 init container 이미지는 `t9_render`에서 온다.
두 위치 모두 목적지에서 실행 가능한 이미지인지 확인한다. 호스트는 목적지에
설치한 T9 렌더러를 사용해야 한다. 로컬 ARM 매핑 보정으로 시험했다면 원본
렌더러와 수정 한 줄의 해시 및 원 실행 결과를 함께 남긴다.

보고서·마지막 출력·시도 namespace와 관리 namespace의 시험 객체 정리 결과를
원 요청에 회신한다. 고정 제안 시험은 실제 AI 제안이나 제품 사람 승인과 다르다.
T15 판정과 완료 체크는 기존 담당이 수행한다.
