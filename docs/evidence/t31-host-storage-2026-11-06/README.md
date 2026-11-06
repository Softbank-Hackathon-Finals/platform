# T31 호스트 저장소 검증 — 2026-11-06

증빙 등급은 **플랫폼 RKE2의 저장소 실측**이다. 업무 앱 반영이나 T19 PASS 증빙이 아니다.
플랫폼의 기존 단일 노드에서 별도 local PV/PVC를 생성했다. 기존 DB 볼륨은 사용하지 않았다.

| 관측 | 결과 |
|---|---|
| 03:31 KST writer Job | 비루트·자격 미마운트 Pod에서 합성 marker fsync 후 저장 |
| writer 중복 프로세스 | 같은 파일 잠금 획득 거부 |
| 03:33 KST reader Job | 다른 Pod UID에서 같은 PVC를 읽기 전용 마운트해 동일 바이트 확인 |
| writer/reader 기록 SHA-256 | `aadbdac4b6c788f26bc7c95cfb506d15dcf99f93dbd17b841a4ec54e9c28f229` |
| 자격 값 조회·업무 변경 | 둘 다 0 |
| 보존 | PV `Retain`, 기존 root 디스크의 독립 경로 |
| 03:37 KST 정리 | Job UID를 대조해 삭제, 임시 Pod·Job 0개. probe 파일·ConfigMap·SA 제거, PV/PVC와 실행 기록 디렉터리 보존 |

원본의 클러스터·노드·PV/PVC·Pod·Job 식별자와 SSM 실행 참조는 비공개 운영 영수증에
보존했다. 공개 표에는 식별자를 싣지 않았다. 전후 PV/PVC UID가 같고 두 Pod UID가
다르다는 조건을 실제 조회에서 대조했다.

재현 코드는 [storage probe](../../../infra/envs/apply-host/probe.py)와
[storage renderer](../../../infra/envs/apply-host/render.py)다. 실제 설치 계약과 제한은
[실행 호스트 안내](../../../infra/envs/apply-host/README.md)를 따른다.

이 결과는 Pod 사이의 파일 보존과 local filesystem 잠금만 증명한다. T31의 실제
STARTED/APPLIED 영수증을 운영 승인으로 만든 시험, 노드 손실 복원, HA·백업 시험이 아니다.
T10 첫 승인 접수/재조회·프로세스 중단 동작은 별도의 SQLite/PostgreSQL 통합 단위 시험이다.

현재 `demo-gate:1`은 T31 요청의 `ENC-01.1`을 포함하지 않는다. 호스트는 이를 자동으로
추가하거나 `full:1`로 바꾸지 않고 거부한다. 정본 프로필 합의, 현재 인증된 T15 후보/S2 승인,
목적지 자격 회전과 T19 생산자 인수가 있어야 실제 반영/게이트 인수를 진행할 수 있다.
