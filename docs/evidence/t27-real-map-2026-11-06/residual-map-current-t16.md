# 잔존 의존성 지도: demo-chat → onprem-demo

- Exit Drill 판정: **NOT_RUN**
- 후보 적합성(T16): needs_work
- 인벤토리: `sha256:a22a288d762581e6fa907e3a75e8a093a1ae01f25b5e3a929fe6d166b8ea8e9b`
- 차단 계획: `sha256:78a1180704e1c68f60aeac38e0a95b20e91345d79d7e379a5243095736773508`

## 인벤토리 추적 메모 (1)

T16이 배포를 따라가며 남긴 메모다. 읽지 못한 입력도 여기 나온다.

- Deployment/source-pgbouncer → ServiceAccount/default: IRSA 역할 주석 없음

## 남음 (5)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| cloudflare (edge · tunnel) | 등급 없음 | edge (app=bravo-edge-tunnel) | - | NOT_RUN | - |
| ghcr.io (image pull) | 등급 없음 | - | - | NOT_RUN | - |
| github (코드 · 이미지 빌드) | 등급 없음 | - | - | NOT_RUN | - |
| tailscale (복제 · 제어 경로) | 등급 없음 | - | - | NOT_RUN | - |
| 모델 공급자 (플랫폼의 에이전트 워커) | 등급 없음 | - | - | NOT_RUN | - |

## 불명 (판정 못 함) (2)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| kms (kms) | 코드 수정 | openbao-transit | migration_omission | NOT_RUN | block:aws-kms:app |
| secrets (secrets) | 코드 수정 | openbao+eso | migration_omission | NOT_RUN | block:aws-secretsmanager:app |

## 유지 (복제 채널) (2)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| 복제 채널 replication-forward (Tailscale로 출발지 DB와 연결) | 등급 없음 | - | - | NOT_RUN | keep:replication-forward:db |
| 복제 채널 replication-reverse (Tailscale로 출발지 DB와 연결) | 등급 없음 | - | - | NOT_RUN | keep:replication-reverse:stream |

## 차단 대상 아님 (이 시험으로 확인 안 됨) (1)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| objects (app.kubernetes.io/name=target-seaweedfs) | 등급 없음 | - | - | NOT_RUN | - |

## 대체 있음 · 아직 확인 안 됨 (4)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| db (db) | 설정 교체 | cnpg | - | NOT_RUN | block:pooler-to-source:pooler |
| iam (iam) | 어댑터 추가 | k8s-rbac+openbao+seaweedfs-policy | - | NOT_RUN | block:aws-iam:app, block:aws-sts:app |
| object_store (s3) | 설정 교체 | seaweedfs | - | NOT_RUN | block:aws-s3:app |
| secrets (secretsmanager) | 코드 수정 | openbao+eso | - | NOT_RUN | block:aws-secretsmanager:app |

