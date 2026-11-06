# 저장소 키를 받지 않고 열기·닫기 시험하기

개인 팀 접속으로 클러스터 안의 고정 작업을 요청한다. 기존 T9 `object_exposure.py`의 렌더·probe를 사용한다. 기존 키·계정 이름·버킷 권한을 보존하며, 열기에서는 첨부 버킷의 익명 읽기만 추가한다. 키나 Secret 파일을 로컬에 내려받거나 환경 변수로 전달하지 않는다.

## 실행과 결과

다른 첨부 복사·검증과 겹치지 않는 작업 창에서 실행한다. 열기와 닫기 때 SeaweedFS를 각각 재시작하므로 첨부 접근이 잠시 중단된다. 일반 상태는 닫힘이다. `cycle`만 명시적 익명 읽기 확인을 받아 잠깐 열고 반드시 닫기·정리로 진행한다. 팀 배포물에서는 같은 명령을 `bash bravo objects ...`로 실행한다.

```sh
# 읽기 전용 사전 확인
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

`--wait-seconds 240`을 추가하면 결과를 기다린다. 기다리는 터미널을 닫아도 서버 작업은 계속된다. 기본 접수는 run ID를 즉시 반환하고 다음 분의 작업 주기에 시작한다. `--hold-seconds`는 0–60초이며 이 시간 뒤 자동으로 닫는다. 실행 요청은 5분 안에 시작하지 못하면 열지 않고 실패로 남긴다. 응답이 유실되면 접수 전에 출력한 ID로 조회한다.

결과의 `phase`, `verdict`, `cleanup`을 함께 읽는다. `FINISHED`와 `PASS`, `CLEAN`이 정상 시험 완료다. 읽기 전용 `check`의 정리는 `NOT_APPLICABLE`이다. 요청 접수·진행 중 상태는 PASS가 아니다. `status`에 ID를 생략하면 본인의 가장 최근 실행을 찾는다. 다른 기기에서도 본인 계정과 같은 ID로 조회할 수 있다. 서버는 최근 64건의 결과를 보관하며 필요한 JSON은 `status ... > result.json`으로 보존한다. 결과에 저장소 자격이나 객체 본문은 포함하지 않는다.

## 중단 복구와 범위

고정된 CronJob은 한 번에 하나의 작업만 수행한다. 개인 PC 연결이나 개인 단기 자격이 만료돼도 클러스터 내부의 기존 작업이 계속된다. 작업 프로세스가 중단되면 다음 주기는 남은 실행을 다시 열지 않고 닫기만 복구한다. Kubernetes가 멈추거나 API·스토리지가 계속 응답하지 않으면 닫힘을 보장할 수 없다. 이때는 `RECOVERY_REQUIRED`와 `UNCONFIRMED`를 유지하고 같은 실행의 닫기를 재시도한다. 처리 중 새 시험은 거부한다. 임의 새 run으로 실패 이력을 지우지 않는다.

닫기는 닫힌 Secret 적용, Deployment Ready 확인, 첨부 버킷 정책의 부재 확인/기존 정책 제거, 익명 GET 403, 기존 첨부의 정상 인증 읽기와 바이트 대조, 이번 실행의 객체 삭제·부재 확인 순서다. 시험 객체는 예측 불가능한 run ID의 전용 경로에만 만든다. 기존 첨부, 다른 버킷 객체, PVC와 namespace는 삭제하지 않는다.

결과는 이 저장소 운영 시험의 근거다. 앱의 전체 첨부 UI, 실제 AI 제안, T14 게이트, 승인·컷오버 성공을 대신하지 않는다. 한 운영자의 실제 실행과 다른 팀원의 기기에서 실제로 실행한 결과도 구분한다.

## 최초 설치와 변경

환경 담당이 설치를 한 번 수행한다. 기존 개인 접속과 해당 개발 클러스터의 운영 권한을 사용하며 새로운 사람용 저장소 키를 발급하지 않는다. 실행 SA는 이름이 고정된 상태 ConfigMap, 저장소 Secret, Deployment에만 get/patch와 클러스터 UID 읽기 권한을 가진다. 앱의 객체 자격 Secret은 읽기만 허용하고 같은 클라이언트인지 메모리에서 대조한다. 이 구성은 기존 팀 개발 클러스터의 관리자 권한을 축소하는 IAM 전환은 아니다.

```sh
uv run --python 3.12 python scripts/object_operations.py install \
  --cluster-uid 실제확인한클러스터UID --access team-test.json
```

대상 클러스터·Secret·Deployment UID를 고정한다. 설치 상태는 IDLE이며 자동으로 열거나 재시작하지 않는다. 소스 ConfigMap은 내용 해시 이름과 immutable 설정으로 고정한다. 진행 중 설치 변경은 거부한다. 다른 UID의 교체 리소스를 자동 인수하거나 기존 실행 기록을 초기화하지 않는다.

키는 작업 안에서 기존 Secret을 읽어 메모리에서만 소비한다. 운영 중 계정 추가·키 회전·권한 변경으로 T9 렌더 계약과 달라지면 자동으로 버리지 않고 중단한다. 기존 `target-object-bootstrap`/`target-object-app` 이름과 새 `bootstrap`/`client` 이름은 같은 계약으로 인정하되 원래 이름을 유지한다. T15 rehearsal identity가 이미 있다면 함께 보존한다.
