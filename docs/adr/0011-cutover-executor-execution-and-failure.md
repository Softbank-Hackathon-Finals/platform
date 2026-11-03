# [ADR-0011] Cutover Executor: 실행 기록과 실패 판정

* **상태 (Status):** 제안됨(Proposed). 모의 환경(k3d) 실측 완료, T8 · T30 실제 환경 확인 후 채택
* **날짜 (Date):** 2026-11-03
* **관련 (Related):** T12 #22, T10 #19, T20 #30, T25 #35, 설계 문서 8.6 · FR-9 · NFR-1 · NFR-3, [ADR-0006](0006-cutover-executor-language.md)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 설계 문서 8.6은 컷오버 8단계 순서, 실패 3분기(ABORT_PRE_WRITE · FAILBACK · SAFE_HOLD), Executor 호출 계약(Idempotency-Key, epoch, 응답 유실 시 상태 다시 읽기)을 정했다. 구현에서 정할 것이 남았다: 실행 기록을 어디에 두는가, 프로세스가 죽었다 뜨면 무엇을 하는가, 원격 조작을 어떤 경로로 하는가.
* **문제:** 쓰기 정지 구간에서 판정을 잘못하면 출발지 · 목적지 DB가 동시에 쓰기를 받는다. 진입점 차단과 PgBouncer PAUSE는 프로세스 메모리에만 있어 파드가 재시작하면 풀린다.
* **목표:** 어느 순간에도 업무 쓰기를 받는 DB는 최대 하나이고, 상태를 확인할 수 없으면 아무것도 새로 열지 않는다.

## 2. 고려한 옵션들 (Considered Options)

### 1. 안전 판정의 기준
| 옵션 | 판단 |
|---|---|
| 진입점 차단 · PAUSE 상태 | 재시작하면 풀린다(demo-chat `WRITE_BLOCKED` 시작값, PgBouncer 재시작). 기준이 될 수 없다 |
| **DB 앱 역할의 로그인 가능 여부** | DB에 저장되어 재시작에도 남는다. **채택** |

### 2. 실행 기록 · lease
| 옵션 | 판단 |
|---|---|
| T10 테이블 공유 | T10 일정에 묶인다 |
| **플랫폼 DB의 `executor` 스키마, Executor 소유** | **채택**. lease는 DB 쌍마다 PENDING · RUNNING · SAFE_HOLD 실행을 하나로 제한하는 부분 유니크 인덱스. 시간 만료 lease는 쓰지 않는다(만료로 다음 실행이 들어오면 PAUSE된 상태 위에서 시작할 수 있다) |

### 3. 재시작 뒤 판정
| 옵션 | 판단 |
|---|---|
| 확정 뒤면 남은 단계를 이어서 진행 | 재시작 판정이 커지고 시험할 경우가 늘어난다 |
| **확정 전 + 목적지 닫힘 확인이면 원상 복구(ABORT_PRE_WRITE), 그 밖은 SAFE_HOLD** | **채택**. NFR-3과 가장 단순하게 맞는다 |

### 4. 진입점 쓰기 차단 경로
| 옵션 | 판단 |
|---|---|
| K8s API 서버 프록시로 파드의 `/admin/write-block` 호출 | **실측 불가**: 프록시가 Authorization 헤더를 파드에 넘기지 않아 401 |
| 파드 IP 직접 호출 | 플랫폼에서 두 클러스터 파드 대역으로 경로가 필요 |
| **K8s API exec로 파드 안에서 `127.0.0.1:8080` 호출** | **채택**. 파드의 토큰을 그대로 써서 Executor가 앱 토큰을 몰라도 된다 |

## 3. 결정 사항 (Decision Outcome)

* **안전은 DB 앱 역할 로그인으로 판정한다.** 진입점 차단 · PAUSE는 쓰기 정지 중 요청을 503 · 대기로 처리하는 수단이다.
* **호출은 비동기다.** `POST /v1/cutovers`는 기록을 남기고 202를 돌려준다. 같은 키 재요청은 저장된 실행을 200으로, 같은 키 다른 입력은 409. `GET /v1/cutovers/{id}`, `?plan_id=&epoch=`로 상태를 읽는다. 단계 상태 값은 T10 `execution.updated`와 같다(started · succeeded · failed · unknown).
* **epoch · lease는 DB 쌍 단위다.** 되돌리기(T20)도 같은 범위다. 받은 최댓값 이하 epoch는 거부한다. SAFE_HOLD는 사람이 풀 때까지 lease를 쥔다.
* **6단계는 두 번 기록한다.** 구독을 바꾸기 전 `stage6_entered_at`, 바꾼 뒤 `committed_at`. 확정 기록 전 실패는 구독까지 원래대로 되돌린다.
* **SAFE_HOLD는 아무것도 새로 열거나 닫지 않는다.** 이미 열린 목적지를 다시 닫지 않는다(출발지는 확정 전에 NOLOGIN이 확인됐으므로 쓰기 DB는 하나다). 양쪽 상태를 읽어 결과에 남기고, 읽지 못한 쪽은 `unknown`.
* **Executor 프로세스는 하나다(advisory lock).** 시작할 때 끝나지 않은 실행부터 판정한다. PENDING은 원격 효과가 없어 ABORT_PRE_WRITE.
* **실행마다 소유자를 기록하고, 원격 상태를 바꾸기 전에 소유를 확인한다.** 재시작 판정은 RUNNING 실행을 넘겨받는다(소유자 교체). 잠금 연결이 끊긴 앞 Executor가 아직 살아 있어도 단계 시작 · 목적지 열기 · 출발지 복원 직전에 소유를 잃은 것을 보고 아무것도 하지 않고 멈춘다. 최종 상태도 소유자일 때만 쓴다. 확인과 조작 사이의 수 밀리초 틈은 남는다(P1 클러스터별 에이전트에서 줄인다).
* **PAUSE 시간 초과만으로 RESUME하지 않는다.** 일시정지는 걸린 채로 두고, 목적지가 닫혀 있음을 확인한 원상 복구 절차가 RESUME한다.
* **deadline은 새 실행에서만 검사한다.** 같은 키 재요청과 재시작 판정은 지난 deadline이어도 저장된 요청을 읽는다.
* **T13 연결 전 비교는 `UNVERIFIED`다.** 모의 환경 설정(`allow_unverified_compare`)이 아니면 DONE이 되지 않는다.

**이유**
* 재시작 · 응답 유실 · 프로세스 종료가 섞여도 "쓰기 DB 최대 하나"를 DB 상태 하나로 확인할 수 있다.
* 판정 규칙이 단순해 시험으로 경우를 다 덮을 수 있다(아래 결과).

## 4. 결과

**구성** (`services/cutover-executor/`)
* `runner.py`: 8단계, 실패 분기, `recover()`. `store.py`: 실행 기록. `remote/`: PgBouncer · DB · K8s 어댑터
* `testing/mockenv.py`: k3d에 출발지 · 목적지(Postgres 16, PgBouncer 1.26.1, demo-chat 2개) · moto
* `testing/sql/10-roles.sql`: Executor 제어 역할 `cutover_control`의 최소 권한 초안

**모의 환경 실측 (2026-11-03, k3d v1.35.5, 부하 없음)**

| 항목 | 결과 |
|---|---|
| 정상 경로 쓰기 정지(첫 쓰기 제한 ~ 마지막 경로 ACK) | 약 1.0초 (진입점 차단 0.27초, PAUSE 0.03초, 펜싱 0.05초, 비교 0.08초, 재연결 0.24초, 해제 0.28초) |
| 진입점 차단 exec를 파드마다 순서대로 할 때 | 4.4초 → 파드마다 동시 실행으로 1.0초 |
| ABORT_PRE_WRITE | 긴 트랜잭션, PAUSE deadline 초과, barrier 미도착, 6단계 구독 변경 뒤 실패, MISMATCH 각 1회: 출발지 쓰기 → 목적지 복제, 목적지 쓰기 거부 |
| 재시작(프로세스 종료 흉내) | 2 · 4 · 5 · 6단계 전 → ABORT_PRE_WRITE, 7단계 전 · 목적지 연 직후 → SAFE_HOLD |
| SAFE_HOLD | 7단계 직전 목적지 DB · 직후 목적지 PgBouncer 접속 장애 → 여는 조작 없음, 상태 기록 |
| SAFE_HOLD 중 PgBouncer · 앱 파드 재시작 | PAUSE · 차단이 풀려도 출발지 NOLOGIN으로 쓰기 거부, 양쪽 메시지 수 변화 없음 |
| 모든 시험의 쓰기 DB 감시(20ms 간격, 로그인 가능 또는 앱 세션 남음 = 쓰기 가능, 읽지 못한 표본이 있으면 실패) | 두 DB가 동시에 쓰기 가능한 순간 0회 |
| 잠금을 잃은 앞 Executor와 넘겨받은 Executor가 겹침(펜싱 전 · 재연결 전) | 앞 Executor는 다음 조작 전에 멈춤, 넘겨받은 쪽 판정(ABORT · SAFE_HOLD)만 남고 목적지는 닫힌 채 |

**알게 된 것**
* PgBouncer PAUSE가 끝나지 않는 동안에도 상태는 paused=1이다. PAUSE를 보낸 연결을 끊어도, 트랜잭션이 끝나도 그대로이고 RESUME해야 풀린다. RESUME해도 PAUSE를 보낸 연결은 응답을 받지 못하므로 시간 초과 때 그 소켓을 직접 끊는다.
* PgBouncer는 서버 로그인 실패를 `server_login_retry`(기본 15초) 동안 기억해 바로 거절한다. 펜싱 중 재시작된 PgBouncer는 출발지 로그인을 복원한 뒤에도 15초간 쓰기를 거절했다. ABORT 복구 확인을 최대 20초 재시도한다.
* 구독 적용 작업자는 구독 소유 역할로 로그인한다. 소유 역할에 LOGIN이 필요하다(비밀번호 없음).
* 역방향 구독의 복제 슬롯은 꺼져 있는 동안의 목적지 로컬 변경을 모아 두었다가 켤 때 보낸다. 목적지에 직접 넣은 행이 출발지 id와 겹치면 역방향 복제가 멈춘다.

**남은 일**
* T13 비교 연결(`Comparer`), 한 행 변조 시 ABORT 확인
* T8 · T30 실제 환경 컷오버 1회(완료 기준), 제어 역할 권한 확정(김태현), PgBouncer 설정 볼륨 · `server_login_retry` · 파드 1개 제약(류지환 · 김태현)
* WebSocket 1012 종료(demo-chat 관리 API, spec D5), SAFE_HOLD를 사람이 푸는 절차
