# 잔존 의존성 지도: demo-chat → onprem-demo

- Exit Drill 판정: **PASS**
- 후보 적합성(T16): needs_work
- 인벤토리: `sha256:55271b9513e48c23c1a730faeebd44fc6831a1e1e57c9ef14d59641b60ccbc01`
- 차단 계획: `sha256:3ef14c833eb0df6883f19d488bb889835e6d83465e0e6ded2c56a25424151a7f`

## 남음 (5)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| cloudflare (edge · tunnel) | 등급 없음 | - | - | NOT_RUN | - |
| ghcr.io (image pull) | 등급 없음 | - | - | NOT_RUN | - |
| github (코드 · 이미지 빌드) | 등급 없음 | - | - | NOT_RUN | - |
| tailscale (복제 · 제어 경로) | 등급 없음 | - | - | NOT_RUN | - |
| 모델 공급자 (플랫폼의 에이전트 워커) | 등급 없음 | - | - | NOT_RUN | - |

## 불명 (판정 못 함) (1)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| kms (kms) | 코드 수정 | openbao-transit | migration_omission | PASS | block:aws-kms:app |

## 유지 (복제 채널) (2)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| 복제 채널 replication-forward (Tailscale로 출발지 DB와 연결) | 등급 없음 | - | - | PASS | keep:replication-forward:db |
| 복제 채널 replication-reverse (Tailscale로 출발지 DB와 연결) | 등급 없음 | - | - | PASS | keep:replication-reverse:stream |

## 차단 대상 아님 (이 시험으로 확인 안 됨) (1)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| objects (app.kubernetes.io/name=target-seaweedfs) | 등급 없음 | - | - | NOT_RUN | - |

## AWS 경로 막힘 · 앱 동작 확인 (4)

| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |
| --- | --- | --- | --- | --- | --- |
| db (db) | 설정 교체 | cnpg | - | PASS | block:pooler-to-source:pooler |
| iam (iam) | 어댑터 추가 | k8s-rbac+openbao+seaweedfs-policy | - | PASS | block:aws-iam:app, block:aws-sts:app |
| object_store (s3) | 설정 교체 | seaweedfs | - | PASS | block:aws-s3:app |
| secrets (secretsmanager) | 코드 수정 | openbao+eso | - | PASS | block:aws-secretsmanager:app |

