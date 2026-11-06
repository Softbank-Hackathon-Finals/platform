# 저장소 키를 받지 않고 열기·닫기 시험하기

개인 팀 접속으로 클러스터 안의 고정 작업을 요청한다. 기존 T9 `object_exposure.py`의 렌더·probe를 사용한다. 기존 키·계정 이름·버킷 권한을 보존하며, 열기에서는 첨부 버킷의 익명 읽기만 추가한다. 키나 Secret 파일을 로컬에 내려받거나 환경 변수로 전달하지 않는다.

## HTTP API

CLI는 같은 HTTP API를 호출한다. 별도 공유 API 키를 발급하지 않으며 기존 개인 접속 도구가 발급한 단기 토큰으로 인증한다. API 서버가 Kubernetes TokenReview로 검증한 주체를 실행 소유자로 기록한다. 요청 본문에 `owner`, 임의 명령, 이미지, Secret 값, 대상 주소를 넣으면 거부한다.

[OpenAPI 3 명세](object-operations.openapi.json)의 기본 경로는 기존 `team-test.json`의 `direct_server` 뒤에 다음을 붙인다. 클러스터 안 서비스는 `bravo-object-operations.target-dev.svc.cluster.local:8080`이다. 외부에서는 기존 TLS 개인 API의 [Service proxy](https://kubernetes.io/docs/tasks/access-application-cluster/access-cluster-services/)를 경유하므로 새 인터넷 공개 포트나 포트포워딩이 필요하지 않다.

```text
/api/v1/namespaces/target-dev/services/http:bravo-object-operations:http/proxy
```

| 요청 | 동작 |
|---|---|
| `POST /v1/object-exposure/runs` | `check`, `cycle`, `open`, `close` 중 하나 접수 |
| `GET /v1/object-exposure/status` | 희망 상태와 시각이 표시된 마지막 관측 상태 |
| `GET /v1/object-exposure/runs` | 본인의 최근 실행·결과 목록 |
| `GET /v1/object-exposure/runs/{run_id}` | 같은 실행의 상태·검사·시간·정리 결과 |
| `POST /v1/object-exposure/runs/{run_id}/cancel` | `{}`로 취소 요청; 진행 중이면 같은 실행의 닫기로 진행 |
| `GET /openapi.json` | 인증 후 API 명세 조회 |

외부 요청은 `Authorization: Bearer <개인 단기 토큰>`과 `X-Bravo-Authorization: Bearer <같은 토큰>`을 보낸다. 앞쪽 API 인증과 뒤쪽 서비스의 독립 검증에 각각 사용한다. 토큰을 로그나 파일에 쓰거나 다른 사람에게 전달하지 않는다. 서비스는 호출자가 주장하는 사용자 이름 헤더를 신뢰하지 않는다. 인증이 없거나 만료되면 401, 팀 주체가 아니면 403, 남의 실행 조회·취소는 404다.

접수에는 `Idempotency-Key: <현재 Unix 초>-<UUID hex 32자>`가 필요하다. 아래 Python 클라이언트의 `new_key()`로 만들 수 있다. 응답을 받지 못해도 **같은 키와 같은 본문**을 쓰면 기존 run을 반환한다. 같은 키에 다른 입력은 409다. 다른 실행이 진행 중일 때도 409이며, 새 키로 계속 재시도하지 않는다. 오래되어 결과 보관 범위를 벗어난 키는 410으로 거부해 다시 실행하지 않는다. `202`는 접수이며 성공 판정이 아니다.

```json
{
  "action": "cycle",
  "hold_seconds": 15,
  "acknowledge_anonymous_read": true
}
```

다른 Python 도구에서도 실행기를 복제하지 않고 [HTTP 클라이언트](../scripts/object_operations/client.py)를 사용한다. 기존 개인 인증 래퍼 안에서 실행하면 토큰은 메모리/프로세스 환경으로만 전달된다. 아래는 읽기 전용 예다.

```python
import json
from pathlib import Path
from object_operations.client import Client

api = Client(json.loads(Path("team-test.json").read_text()))
request_key = api.new_key()  # 재조회에 필요한 비밀값 없는 요청 키를 먼저 보존
result = api.call("POST", body={"action": "check"}, key=request_key)
print(result["run_id"])
print(api.call("GET", "/" + result["run_id"]))
```

```sh
PYTHONPATH=scripts uv run --python 3.12 python infra/envs/rehearsal-access/team_test.py \
  --profile team-test.json -- python your_tool.py
```

API는 별도 SA로 실행하며 상태 ConfigMap get/patch, TokenReview create, 클러스터 UID 읽기만 허용한다. 저장소 Secret 읽기와 Deployment 변경은 고정 작업의 SA에만 있다. 운영자는 여전히 기존 개발 클러스터의 팀 권한을 사용한다. 이 API를 설치했다고 다른 기존 권한까지 축소했다고 주장하지 않는다.

## 실행과 결과

다른 첨부 복사·검증과 겹치지 않는 작업 창에서 실행한다. 상태 변경 때 SeaweedFS를 재시작하므로 첨부 접근이 잠시 중단된다. **개발 작업용 `open`은 익명 읽기만 열린 상태로 유지하며 자동으로 닫지 않는다.** `close`를 명시적으로 요청하면 닫는다. `cycle`은 열림/닫힘 시험을 모두 하고 닫는 별도 명령이며, 지속 open 모드에서는 409로 거부한다. 팀 배포물에서는 같은 명령을 `bash bravo objects ...`로 실행한다.

```sh
# 현재 작업 모드와 마지막 관측 상태/시각
uv run --python 3.12 python scripts/team_runtime.py objects state --access team-test.json

# 개발 작업 중 익명 읽기만 계속 허용. 명시적 close 전까지 유지
uv run --python 3.12 python scripts/team_runtime.py objects open \
  --acknowledge-anonymous-read --access team-test.json

# 읽기 전용 실제 상태 확인 (open에서도 실행 가능)
uv run --python 3.12 python scripts/team_runtime.py objects check --access team-test.json

# 열기 → 익명 경계 검사 → 15초 유지 → 닫기/정책 제거 → 정상 읽기 → 시험 객체 삭제
uv run --python 3.12 python scripts/team_runtime.py objects cycle \
  --acknowledge-anonymous-read --access team-test.json

# 출력된 ID 전체로 조회. 새로 cycle을 실행하면 별도 시험이므로 재전송하지 않는다.
uv run --python 3.12 python scripts/team_runtime.py objects status \
  --run-id objects-실제출력ID --access team-test.json

# 진행 중인 자신의 실행에 닫기·정리를 요청
uv run --python 3.12 python scripts/team_runtime.py objects cleanup \
  --run-id objects-실제출력ID --access team-test.json

# 별도의 작업 창에서 닫힘을 다시 적용하고 정책/정상 읽기 확인
uv run --python 3.12 python scripts/team_runtime.py objects close --access team-test.json
```

`--wait-seconds 240`을 추가하면 결과를 기다린다. 기다리는 터미널을 닫아도 서버 작업은 계속된다. 기본 접수는 run ID를 즉시 반환하고 다음 분의 작업 주기에 시작한다. `--hold-seconds`는 `cycle`의 열린 유지 시간(0–60초)이다. `open`에는 자동 닫힘 시간이 없다. 이미 열린 상태의 open은 상태를 확인하고 불필요하게 재시작하지 않는다. 실행 요청은 5분 안에 시작하지 못하면 열지 않고 실패로 남긴다. 응답이 유실되면 접수 전에 출력한 ID로 조회한다.

결과의 `phase`, `verdict`, `cleanup`, `final_state`를 함께 읽는다. `FINISHED`와 `PASS`, `CLEAN`은 요청한 작업과 시험 객체 정리 완료다. **버킷이 닫혔다는 뜻은 아니다.** 지속 open의 `final_state`는 `open`, `security_state`는 `PUBLIC_READ`다. 이 작업의 PASS는 보안 게이트 PASS가 아니며 익명 접근 통제 검사는 BLOCK이 정상이다. 읽기 전용 `check`의 정리는 `NOT_APPLICABLE`이다. 요청 접수·진행 중 상태는 PASS가 아니다. `status`에 ID를 생략하면 본인의 가장 최근 실행을 찾는다. 다른 기기에서도 본인 계정과 같은 ID로 조회할 수 있다. 서버는 최근 64건의 결과를 보관하며 필요한 JSON은 `status ... > result.json`으로 보존한다. 결과에 저장소 자격이나 객체 본문은 포함하지 않는다.

## 중단 복구와 범위

고정된 CronJob은 한 번에 하나의 작업만 수행한다. 개인 PC 연결이나 개인 단기 자격이 만료돼도 클러스터 내부의 기존 작업이 계속된다. 성공한 지속 open은 작업 완료 뒤 그대로 유지한다. 완료 전에 작업 프로세스가 중단되면 다음 주기는 불명확한 실행을 다시 열지 않고 닫기만 복구하며 원 작업을 PASS로 만들지 않는다. Kubernetes가 멈추거나 API·스토리지가 계속 응답하지 않으면 닫힘을 보장할 수 없다. 이때는 `RECOVERY_REQUIRED`와 `UNCONFIRMED`를 유지하고 같은 실행의 닫기를 재시도한다. 처리 중 새 시험은 거부한다. 임의 새 run으로 실패 이력을 지우지 않는다.

닫기는 닫힌 Secret 적용, Deployment Ready 확인, 첨부 버킷 정책의 부재 확인/기존 정책 제거, 익명 GET 403, 기존 첨부의 정상 인증 읽기와 바이트 대조, 이번 실행의 객체 삭제·부재 확인 순서다. 시험 객체는 예측 불가능한 run ID의 전용 경로에만 만든다. 기존 첨부, 다른 버킷 객체, PVC와 namespace는 삭제하지 않는다.

결과는 이 저장소 운영 시험의 근거다. 앱의 전체 첨부 UI, 실제 AI 제안, T14 게이트, 승인·컷오버 성공을 대신하지 않는다. 한 운영자의 실제 실행과 다른 팀원의 기기에서 실제로 실행한 결과도 구분한다.

## 최초 설치와 변경

`state`/HTTP 상태 GET은 마지막 관측 시각을 함께 반환하며 실시간 S3 검사라고 표시하지 않는다. `check`를 요청하면 현재 configured state와 실제 익명/인증 접근의 일치를 다시 확인한다.

환경 담당이 설치를 한 번 수행한다. 기존 개인 접속과 해당 개발 클러스터의 운영 권한을 사용하며 새로운 사람용 저장소 키를 발급하지 않는다. 실행 SA는 이름이 고정된 상태 ConfigMap, 저장소 Secret, Deployment에만 get/patch와 클러스터 UID 읽기 권한을 가진다. 앱의 객체 자격 Secret은 읽기만 허용하고 같은 클라이언트인지 메모리에서 대조한다. 이 구성은 기존 팀 개발 클러스터의 관리자 권한을 축소하는 IAM 전환은 아니다.

```sh
uv run --python 3.12 python scripts/object_operations.py install \
  --cluster-uid 실제확인한클러스터UID --access team-test.json
```

대상 클러스터·Secret·Deployment UID를 고정한다. 설치 상태는 IDLE이며 자동으로 열거나 재시작하지 않는다. 소스 ConfigMap은 내용 해시 이름과 immutable 설정으로 고정한다. 진행 중 설치 변경은 거부한다. 다른 UID의 교체 리소스를 자동 인수하거나 기존 실행 기록을 초기화하지 않는다.

키는 작업 안에서 기존 Secret을 읽어 메모리에서만 소비한다. 운영 중 계정 추가·키 회전·권한 변경으로 T9 렌더 계약과 달라지면 자동으로 버리지 않고 중단한다. 기존 `target-object-bootstrap`/`target-object-app` 이름과 새 `bootstrap`/`client` 이름은 같은 계약으로 인정하되 원래 이름을 유지한다. T15 rehearsal identity가 이미 있다면 함께 보존한다.
