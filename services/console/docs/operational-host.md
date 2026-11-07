# T21 운영 호스트 연결 준비

기존 Sova UI를 유지하고, T10/T15 단일 호스트에 T21을 합성하는 선택적 진입점이다.
**이 코드의 병합만으로 운영 배포·신원 등록·고객 수집 연결은 완료되지 않는다.**

## 재사용하는 경계

- `bravo_console.server`는 기존 `approval_server.load_host`와 `operator_ports.create`를 사용한다.
  별도 실행기·판정기·DB·승인 원장을 만들거나 스키마를 초기화하지 않는다.
- 운영자 신원은 기존 `OperatorSsoVerifier`, 계획별 권한은 기존 `OperatorAccess`로 확인한다.
  신원 등록과 grant 변경을 수행하는 HTTP API는 추가하지 않는다.
- 보안 비교는 기존 T10 `GateRecords`와 T19 Ed25519 출처 검증을 읽는다. 서명이 없거나 권한·계획·유효기간이 맞지 않으면 결과를 공개하지 않는다.
- IaC 요청·승인·PR 생성은 기존 T44/T10 API만 사용한다. 배포 설정에 실제 GitHub App이 없으면 PR 생성 경로를 대신 만들지 않는다.
- 고객 상태는 고정된 `infra/iac/customer_status.py`와 TLS를 검증하는 Kubernetes reader로 수집한다.
  상태 JSON이나 hash를 브라우저가 제출해 생산자로 등록할 수 없다.
- 워크로드 대응·탐지·네트워크·감사·확장 실행 공급자는 이 진입점에서 아직 연결하지 않았다.
  T19 FAIL을 공격 탐지나 실제 격리 성공으로 바꾸지 않는다. 미연결 기능은 기존 UI의 미설정 상태를 유지한다.

## 호스트 입력

기존 호스트의 보호된 입력과 동일한 프로세스·DB·권한을 유지한다. 콘솔 입력은 별도 읽기 전용 파일이다.

```json
{
  "version": 1,
  "public_origin": "http://127.0.0.1:18123",
  "modules": [],
  "customers": []
}
```

`modules`는 지금 `[]`만 받는다. 모듈 등록에는 tenant가 없고 환경 ID는 tenant마다 겹칠 수 있어서, 여러 tenant가 쓰는 호스트에서는
다른 tenant 계획에 모듈 이름과 `params`가 보이기 때문이다. tenant 기준이 생기기 전에 값을 넣으면 시작하지 않는다.
`customers`에는 운영자가 검토한 `tenant_id`, `environment_id`, `environment`, `cluster_uid`, `reviewed_revision`,
절대경로 `kubeconfig`만 등록한다. 실제 등록 원문을 사용하며 예시 UID를 실제 대상으로 채택하지 않는다.
환경 ID는 tenant 안에서만 고유하다. 그래서 관측은 계획의 tenant(`tenant_for`)가 등록 `tenant_id`와 같고,
그 환경이 계획의 출발지·목적지 쌍에 있을 때만 내준다. 아니면 그 고객 항목은 목록에서 빠진다(환경 이름·검토 revision도 보이지 않는다).
권한은 이 파일에서 부여하지 않는다. 데이터 출처용 source SHA와 코드 hash도 이미지 빌드 입력으로 고정해야 한다.

```sh
python -m bravo_console.server --config <기존-host.json> --console-config <보호된-console.json> \
  --tls-cert <기존-서버인증서> --tls-key <기존-서버키>
```

공식 이미지에 콘솔과 고정 고객 수집기, `/app/runtime-source.json`이 포함되어야 한다.
진입점은 이 manifest의 source SHA 형식과 수집기 바이트 hash를 검사한 뒤에만, hash를 계산한 그 바이트를 실행한다
(파일을 다시 읽지 않는다).
빌드/차트 연결은 T43 담당 범위 합의 뒤 진행한다. 기존 호스트는 단일 writer·Recreate를 유지하고,
실행 중인 작업과 전환 시간을 확인하기 전에는 변경하지 않는다.

## 개인 SSO 접속 브리지

브라우저가 AWS proof를 보유하게 하지 않는 내부 CLI 접속 도구다. 공개 기업 로그인/IdP 구현이 아니다.
`bravo-personal`, 서울 리전, 기존 operator audience로 매 요청의 짧은 proof를 만든다.

보호된 설정 파일에는 `origin`, `upstream`, `ca_file`, `tls_server_name` 네 항목만 둔다.
`origin`은 고정 loopback 주소이며, `upstream`은 고정 HTTPS 주소다. SSM 등 중계 주소를 사용하더라도
`tls_server_name`을 등록된 서버 인증서 이름으로 고정하고 CA·호스트명 검증을 끄지 않는다.

```sh
python -m bravo_console.operator_bridge --config <보호된-접속설정.json> \
  --session-file <새-비공개-세션파일> --open-browser
```

- 접속 URL은 5분 내 1회만 사용한다. 브라우저에는 HttpOnly·SameSite=Strict 로컬 쿠키만 전달하고 세션은 프로세스 시작부터 최대 15분이다.
- proof는 메모리에서 고정 upstream에만 전달한다. 브라우저·URL·localStorage·접속 로그에 넣지 않는다.
- **읽기 전용이다.** GET(SSE 포함)만 플랫폼에 보낸다. 승인·컷오버·failback·위임·IaC 요청 같은 쓰기는
  콘솔 헤더를 갖춰도 브리지에서 403으로 막는다. 쓰기는 T21이 결정을 결속 값과 함께 보여 주고 T10이 열 경로를
  정한 뒤에 경로별 허용 목록으로 연다. 본문이 있는 요청은 거부하고, `Accept`·`Last-Event-ID`만 전달한다.
- 쿠키는 포트로 나뉘지 않아 같은 PC의 다른 `127.0.0.1` 서버도 same-site다. 그래서 모든 요청에
  `Sec-Fetch-Site`가 `same-origin`(이 origin의 요청) 또는 `none`(운영자가 연 접속 URL)이기를 요구한다.
  헤더가 없거나 그 밖의 값이면 거부한다.
- Host를 검사하고 브라우저의 Authorization 대체·외부 경로·리다이렉트를 거부한다. 경로는 보낸 그대로(`raw_path`) 검사한다.
  `%` 인코딩, 역슬래시, 빈 구간(`//`), `.`·`..` 구간이 있으면 400이다. 디코딩된 경로만 보면 `..%2f`가 허용 목록을 빠져나간다.
- SSE는 proof 만료 전에 정상 종료하고 재접속 때 새 proof를 사용한다. WebSocket 공급자는 미연결로 거부한다.
- 접속 파일을 Git·채팅·공개 문서에 공유하지 않는다. 종료하면 로컬 접속 경로도 중단된다.

## 고객 수집과 운영 인수

30초마다 고정 대상의 GET 관측을 갱신한다. 서비스가 캐시를 읽는 시각으로 수집 시각을 바꾸지 않는다.
수집 실패 시 기존 관측과 발급된 읽기 참조를 지우며, 재시작 후 새 관측이 없으면 UNKNOWN이다.
실패는 `customer_collection_failed environment_id=… reason=…`로 로그에 남긴다. 사유는 수집기의 고정 코드
(예: `CLUSTER_IDENTITY_MISMATCH`)이고, 그 밖의 오류는 예외 종류만 적는다. 오류 메시지는 남기지 않는다.
이는 영속 감사 원장이나 실행 승인 근거를 대체하지 않는다.

운영 수집에는 개인 관리자 토큰 대신 기존 T7 단기·회수 가능한 자격 경로에 전용 읽기 계정을 등록해야 한다.
고정 고객 Application, 컨트롤러 상태, 클러스터 UID 확인에 필요한 메타데이터 GET만 허용한다.
Secret 조회·변경·exec·동기화·컷오버 권한은 부여하지 않는다.

| 확인 항목 | 현재 범위 |
|---|---|
| 콘솔 합성·개인 브리지 | 코드와 로컬 계약 시험. 신원·계획·고객 응답은 fixture |
| 플랫폼 상태 | 실제 조회 시 Reader·등록 API·호스트 Ready, GitOps Synced/Healthy. 해당 시각의 snapshot이며 새 콘솔 배포 근거가 아님 |
| 실제 목적지 수집 | 등록 UID 대조 후 native GET 수집기 실행. NOT_INSTALLED, APPLICATION_CRD_MISSING |
| 고객 desired-state SHA | 현재 읽기 시험에서는 후보 SHA. 고객 적용을 승인한 커밋으로 간주하지 않음 |
| GitHub App | 기존 App이 있다는 사용자 확인. 자동 PR 생성만 선택적 미연결로 유지하며 다른 읽기·배포 기능과 분리 |
| 공유 호스트 전환 | T43 범위 합의·공식 이미지·설정/신원 등록·실행 시간 확인 후 인수 필요 |
| 고객 상시 수집 | 전용 읽기 SA와 단기 자격 등록·네트워크 경로·권한 거부 인수 필요 |
| 실제 격리·이전·복구 | 이 코드와 Ready/Sync 상태로 완료 처리하지 않음 |

T21 전체 완료로 체크하지 않는다. 원본 응답·대상 식별자·접속 로그는 비공개 검증 디렉터리에 보존한다.
