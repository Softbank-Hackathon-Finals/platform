# T10 재시작: 등록 목적지의 Job이 두 번 생기는가

2026-11-05 KST, Mac에서 T10 SQLite 접수기를 실행하고 등록된 개발 목적지 RKE2에 시험 Job을 보냈다. **접수 프로세스를 강제 종료한 뒤 같은 작업을 두 번 요청해도 새 Job이 생기지 않았다.** T15 제품 러너와 T38의 인증된 검증 결과는 이 시험에 연결하지 않았다.

| SIGKILL 시점 | 재시작 뒤 같은 실행·시도의 Job 수 | 재제출 횟수 | T10 결과 |
|---|---:|---:|---|
| 접수 커밋 뒤, Kubernetes 생성 요청 직전 | 0 | 0 | 결과 미확인, 다음 게이트 차단 |
| API가 생성한 Job의 UID를 받은 뒤, T10 호출이 반환하기 전 | 1 | 0 | 처음 받은 UID와 일치. Job 성공 후에도 인증된 T15 결과가 없어 다음 게이트 차단 |

**재현 순서.** 등록된 클러스터 UID를 대조한다. 운영자가 새 Restricted 네임스페이스를 만들고 모든 Pod 인바운드·아웃바운드를 막는다. 시험 호스트는 T10이 커밋한 실행 ID와 시도 번호를 Job 라벨로 사용한다. Job에는 API 토큰이나 업무 자격을 넣지 않는다. Kubernetes가 매번 새 이름을 발급하므로 중복 생성이 이름 충돌로 숨지 않는다.

각 경우에 별도 DB의 시험 계획을 리허설 상태에 두고 실제 접수 프로세스를 종료한다. 같은 DB를 새 프로세스에서 열어 원래 키와 다른 키로 차례로 요청한다. 두 응답의 실행 ID를 비교하고, 클러스터에서 그 실행·시도의 Job 수와 UID를 읽는다. 두 번째 경우에는 Job과 Pod의 소유 관계 및 완료도 확인한다. 마지막에는 시험 네임스페이스를 원 UID 조건으로 지우고 부재를 다시 조회했다. [시험한 T10 코드](https://github.com/Softbank-Hackathon-Finals/platform/blob/a3aba4ba7aa6bf79370eb06ef581b3b4487b04f9/services/platform-events/src/bravo_platform_events/rehearsal.py) · [접수 및 재시작 회귀 시험](https://github.com/Softbank-Hackathon-Finals/platform/blob/a3aba4ba7aa6bf79370eb06ef581b3b4487b04f9/services/platform-events/tests/test_rehearsal.py#L96) · [측정값](result.json)

**다음 운영 판단.** Job이 성공했다는 사실만으로 변경을 승인할 수 없다. T15가 같은 실행·시도에서 만든 Job과 네임스페이스를 조회하고, 생성·삭제 UID와 삭제 후 부재를 인증된 결과로 돌려줘야 한다. 그 전에는 기존 시도를 다시 실행하지 않는다. [T15 인수 기준](https://github.com/Softbank-Hackathon-Finals/platform/issues/24)

**자격 검증의 별도 선행 조건.** 측정 당시 등록 목적지 API 서버의 감사 정책은 모든 요청에 대해 `None`이었다. 빈 Secret 생성을 서버 측 dry-run으로 요청했다. 명령은 성공했고 객체는 전후 모두 없었다. 6초 뒤에도 같은 감사 파일은 0바이트였고 해당 이벤트는 0건이었다. [T7 단건 Secret 검사](../../../infra/envs/platform-data/secret_probe/README.md)에 필요한 계정별 감사 기록을 얻을 수 없어 해당 검사는 시작하지 않았다. [T30에서 검증한 정책](../../../services/control-runner/templates/rke2-audit-policy.yaml)은 Secret 접근의 메타데이터만 남긴다. 기존 단일 노드에 적용할 때는 운영 담당자가 [재시작 절차](../../../infra/envs/onprem/README.md)에 따라 작업 시간을 맞춘다. 이 시험에서는 정책과 업무 워크로드를 바꾸지 않았다.
