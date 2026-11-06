# 본인 계정으로 시험 실행·조회·정리

T3 격리 시험과 T15 고정 제안 자격 이전 시험은 같은 진입점에서 실행한다. 개인 접속은 [기존 팀 접속 도구](../infra/envs/rehearsal-access/TEAM-TEST.md), 시험·판정은 기존 T3/T15, 실제 제품 명령은 T10 계약을 사용한다. 이 도구는 운영 승인이나 실제 AI 제안을 대신하지 않는다.

## 사용

macOS, Linux, WSL에서 `uv`, `kubectl`, 본인 계정으로 연결한 Tailscale이 필요하다. `team-test.json`은 기존 팀 접속 배포물의 비밀값 없는 설정이다. 관리자 kubeconfig나 다른 사람의 토큰을 받지 않는다. Python은 아래 명령이 3.12로 준비한다.

```sh
uv run --python 3.12 python scripts/team_runtime.py check --access /path/to/team-test.json
uv run --python 3.12 python scripts/team_runtime.py run --suite isolation --access /path/to/team-test.json
uv run --python 3.12 python scripts/team_runtime.py run --suite credentials --access /path/to/team-test.json
```

첫 출력의 `run_id`로 같은 실행을 다시 조회하거나 정리한다. 기존 실행을 새로 시작하지 않는다.

```sh
uv run --python 3.12 python scripts/team_runtime.py status --run-id team-실제출력ID --access /path/to/team-test.json
uv run --python 3.12 python scripts/team_runtime.py cleanup --run-id team-실제출력ID --access /path/to/team-test.json
```

`team-실제출력ID`에는 실제 출력된 ID 전체를 넣는다. 기본 기록 위치는 `~/.local/state/bravo-team-runs/<run_id>`다. `--state`를 지정했다면 조회·정리에도 같은 경로를 사용한다. 각 작업은 본인 인증으로 새 단기 자격을 받으며 만료된 실행을 재전송하지 않는다. 다른 기기로 기록을 옮길 때는 **해당 실행 폴더 전체**를 안전하게 옮겨야 한다. 서버에 결과 저장소나 자동 재시도 데몬을 추가하지 않는다.

최종 JSON의 `execution`, `verdict`, `cleanup`은 각각 실행 상태·기존 판정·이번 실행 정리 상태다. `run`은 `PASS`와 `CLEAN`이 모두 있어야 종료 코드 0이다. `cleanup`은 정리 성공을, `status`는 조회 성공을 종료 코드로 표시하며 원 시험 판정은 JSON에 보존한다. 중단 후 정리가 성공해도 중단된 시험을 PASS로 바꾸지 않는다. `receipt` 경로의 원장, `isolation.json` 또는 `t15-credential-flow-*.json` 원본 보고서를 읽으면 단계별 결과를 확인할 수 있다. 기존 T15 보고서의 전체 `rehearsal-*` 잔존 목록과 종료 코드는 보존하며, 남의 namespace는 이 도구의 정리 실패에 포함하지 않는다.

## 저장소 열기·닫기

`objects state|check|open|close|cycle|status|cleanup`는 [저장소 HTTP API](object-operations.md#http-api)를 호출한다. 다른 도구는 같은 API와 OpenAPI 명세를 사용할 수 있다. 클러스터 내부 작업이 기존 키를 소비한다. `open`은 개발용 익명 읽기를 유지하고 명시적 `close`로 닫는다. `cycle`의 닫기와 중단 복구는 개인 PC 연결과 무관하게 이어진다. 별도 작업 창과 명시적 익명 읽기 확인이 필요하다. T3/T15의 로컬 원장·정리와 구분한다.

## 환경을 한 번 등록하는 사람의 책임

환경 관리자는 `bravo-team-access/bravo-self-service-runtime` ConfigMap의 `data.environment.json`을 등록한다. 사용자마다 경로를 채우거나 CA·소스 파일을 전달하지 않는다. [입력 검증 코드](../scripts/team_runtime/recipes.py)의 `bravo.team-runtime/v1` 명세는 다음을 포함한다.

- 대상 `kube-system` UID와 노드 아키텍처.
- 후보 앱·OpenBao·Postgres·Python probe·격리 이미지의 digest. localhost 이미지는 모든 스케줄 가능 노드에 있어야 한다.
- 검토한 demo-chat source SHA, 필요한 세 원본 파일, 고정 제안 edits, 후보와 결속된 diff SHA-256.
- 기존 T3 환경 프로필의 서비스 선택자·포트.

CA는 대상의 기존 `bravo-rehearsal-control/target-openbao-ca`에서 읽는다. 동일한 OpenBao digest를 writer와 T9 init container에 공급한다. T9 경로·역할·init 생성 함수는 정본을 로드한다. 이미지·소스·CA 값, 클러스터 UID를 모듈마다 다시 입력하지 않는다. 환경 ConfigMap의 UID·resourceVersion·내용 해시와 사본을 각 실행에 고정한다. 이후 환경 설정 변경이 기존 실행의 정리 대상을 바꾸지 않는다.

기존에 검토한 T15 config를 최초 환경 등록에 사용할 때는 다음 렌더러로 경로를 제거한 ConfigMap을 만든다. 클러스터 변경은 하지 않는다. 생성 내용을 검토한 환경 관리자가 본인 접속으로 create하고 UID·내용을 재조회한다. 변경 시에는 기존 ConfigMap UID·resourceVersion을 대조한다.

```sh
PYTHONPATH=scripts:services/rehearsal-runner/src:services/agent-worker/src uv run --project services/platform-events --frozen --no-default-groups python -m team_runtime.environment \
  --config /private/reviewed-t15-config.json --architecture arm64 > /private/environment.json
```

등록은 반복 시험과 분리된 설치 작업이다. 기존 이미지 발행·반입 경로가 준비한 digest를 소비한다. 임의 소스에서 후보를 빌드하거나 노드에 privileged importer를 설치하지 않는다. 새로운 후보가 필요하면 기존 T7 발행 계약에 따라 발행하고 환경 레시피를 갱신한다. 이 설정의 diff 검사는 후보 이미지 자체의 공급망 검증을 대체하지 않는다.

## 중단과 정리

Kubernetes create 전에 자원 종류·이름·namespace를 기록하고 실행 표식을 붙인다. 응답 UID를 기록하며, 응답이 끊기면 같은 이름과 표식을 읽어 UID를 복구한다. Secret 본문·입력 값·토큰은 원장에 저장하지 않는다. 정리는 기록된 이름·실행 표식·UID를 모두 대조하고 UID 조건 삭제 후 부재를 확인한다. 목록의 접두사만으로 소유권을 추정하지 않는다. 같은 이름의 다른 UID, 다른 실행 표식, 조회 실패는 삭제하지 않는다.

진행 중 실행은 파일 잠금으로 별도 cleanup을 거부한다. 종료·연결 단절 후 같은 본인 계정으로 cleanup을 실행하면 기존 실행만 정리한다. T15가 OpenBao에 값을 쓴 뒤 중단되면 먼저 해당 writer Job을 UID 조건으로 종료하고 종속 Pod의 종료를 기다린다. 기존 T15 cleanup 스크립트로 해당 namespace의 역할·값을 지운 뒤 관리 객체와 namespace를 정리한다. 복구 Job의 응답이 불명확하면 같은 Job을 조회하고 다시 만들지 않는다. 외부 정리 결과를 확인하지 못하면 `CLEAN`으로 표시하지 않는다.

## 반복 수동 연결의 책임 경계

| 반복되던 연결 | 정본 경로와 담당 경계 | 이 도구의 처리 |
|---|---|---|
| 개인 접속·exec·가장 검사 | T7 `team_test.py` | 기존 직접 API와 개인별 단기 토큰 소비 |
| 워크로드 토큰 전달·회전·폐기 | T7 `credentials.py`, `credential_install.py` | 제품 호스트가 기존 exec 인증을 소비; 새 발급기 없음 |
| 시험용 소스·경로·이미지·CA 조립 | 환경 ConfigMap + T3/T15 | 대상 환경에서 자동 해석, diff·아키텍처·로컬 이미지 확인 |
| 결과 파일 찾기·실패와 잔존 혼동 | 실행별 원장 + 원본 시험 보고서 | 같은 run ID로 조회, 실행·판정·정리 분리 |
| 중단 후 관리자에게 정리 요청 | 실행 원장 + UID 조건 삭제 + 기존 Bao cleanup | 본인 재인증 후 같은 실행 정리 |
| 저장소 키 수동 조립·열기/닫기·정책 정리 | T7 운영 연결 + T9 렌더/probe | `objects` 고정 작업, 서버 결과·중단 후 닫힘 복구 |
| 제품 등록·이미지 발행·배포 | T43 GitOps 및 기존 publication 경로 | 기존 배포 소유자가 관리, 시험 도구가 Deployment 수정하지 않음 |
| 실제 계획·AI·컷오버·승인 | T10/T15/T17 제품 호스트 및 콘솔 | 기존 단일 완료 담당이 연결, 고정 시험을 제품 완료로 집계하지 않음 |

완료 검증은 실행한 계정과 기기를 적는다. 한 사람의 실제 실행이나 다른 주체 가장 검사를 다른 팀원의 실제 로그인·기기 성공으로 바꾸어 쓰지 않는다.
