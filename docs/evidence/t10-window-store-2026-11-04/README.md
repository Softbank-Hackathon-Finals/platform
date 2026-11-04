# T10 창 저장 어댑터 검증 · 2026-11-04

기존 계획별 트랜잭션에서 생산자 권한과 현재 계획을 대조하고 T36의 창 검증·저장을 함께 수행하는 범위다. [호스트 연결과 DB 적용 순서](../../contracts/T10-window-storage.md)를 따른다.

| 환경 | 결과 | 검증한 범위 |
|---|---|---|
| 로컬 SQLite + PostgreSQL 14.20 | 전체 서비스 424 passed; 창 전용 41 passed | 실제 별도 연결, 동일 계획 잠금, 권한·계획 변경·만료 대기, 중복/겹침 경합, 보존, 실패 롤백과 commit 응답 유실 |
| AWS 기존 플랫폼 EC2의 격리 DB | 창 전용 40 passed + 별도 강제 재시작/권한 시험 | PostgreSQL 18.4와 SQLite. 로컬 프로세스 제어를 사용하는 재시작 시험 1개는 외부 컨테이너 하네스의 실제 즉시 종료·WAL 복구 시험으로 대체 |
| migration 호환성 | 통과 | v1의 기존 API 계속 동작; 중간 실패 시 테이블 생성·버전 갱신 전부 롤백; v2에서 기존 계획 API 동작; 이전 v1-only 소비자는 초기화 거부 |
| 정리·운영 경계 | 통과 | 자체 컨테이너·snapshot·임시 디렉터리 부재, 기존 platform-state Pod UID·재시작 횟수 동일 |

AWS 실행은 **12:07:13–12:07:37 KST**다. [aws-test.json](aws-test.json)에 공급자 실행 시각(UTC), PostgreSQL 버전, 실제 시험 수, 사용한 모든 입력 SHA-256, 격리·정리 결과를 보존했다. [receipt-manifest.json](receipt-manifest.json)은 비공개 공급자 응답·실행 payload의 hash와 공개 기록의 연결을 남긴다. 현재 소스 바이트를 클라우드에서 실행한 입력 hash와 전부 대조했다.

## 확인한 실패 경계

- 기본 binding 없음, 다른 tenant/계획/생산자/credential, 만료, 권한 회수는 저장 전에 거부한다. 본문의 plan_id로 다른 계획을 선택할 수 없다.
- PostgreSQL에서는 실제 `pg_locks`의 advisory 잠금 대기를 관측한 뒤 권한 회수·계획 변경을 commit하거나 시각을 만료 이후로 옮겼다. 잠금 뒤 현재 값을 다시 읽고 저장을 거부했다.
- 같은 창의 8개 동시 요청은 1개만 저장된다. 서로 다른 시작 시각이어도 겹치는 창 두 개를 동시에 저장하지 않는다. 시차 표현이 다른 같은 시각도 중복으로 판단한다.
- 최신 창을 보존해 오래된 창의 재진입을 막는다. 오래된 최신 창은 T36 실제 판정에서 STALE/UNKNOWN이며 저장 성공을 PASS로 사용하지 않는다.
- runtime 계정의 새 테이블 DDL·UPDATE와 기존 명령 journal DELETE를 모두 거부했다. 새 창 테이블의 보존 삭제만 추가했다.
- 별도 DB의 PostgreSQL 프로세스를 `immediate`로 정지시킨 뒤 시작했다. 창·관리 binding의 WAL 복구 후 같은 창을 읽었고 권한 경계를 다시 확인했다.
- 기반 커밋 `d3b00c8c59d7d6a3a6ed0fff4c4419d1a046bb13`의 실제 `PostgresPlanStore` 코드를 불러 v2 초기화 거부를 확인했다. 호환 소비 코드의 선배포가 필요하다.

## 실행 방법과 한계

로컬 시험은 서비스 README의 `uv sync --locked --all-extras`, Ruff, pytest 명령을 사용한다. 기존 Starlette/httpx deprecation warning 1건은 그대로 남는다.

클라우드 하네스는 `services/platform-events/testing/window_cloud_host.py`다. 운영자가 계정·EC2 소유 경계를 확인한 뒤 고정 소스/시험 JSON을 기존 SSM 경로로 전달했다. Docker가 없는 호스트라 첫 시도는 생성 전에 종료됐다. 이후 ctr의 capability 인자 형식을 고쳤고 실패 시도에서도 자체 자원 제거와 운영 Pod 불변을 확인했다. 실패 기록을 성공으로 덮어쓰지 않았다.

이미 존재하는 CNPG PostgreSQL image digest를 사용했다. standalone containerd 컨테이너 1개에 CPU 1개·메모리 512Mi, 읽기 전용 root, uid 26, capability 없음, no-new-privileges, seccomp, 새 network namespace와 tmpfs를 적용했다. 테스트 프로세스는 `nobody`와 별도 network namespace로 실행했다. 운영 Secret·DB·Kubernetes 자원은 생성·수정하지 않았고, Pod UID/재시작 횟수의 메타데이터만 전후 조회했다. 임시 PostgreSQL superuser는 합성 fixture 초기화만 담당하며 실제 어댑터 호출은 제한된 runtime 역할을 사용했다.

T36 코드는 검토 중인 #168의 고정 커밋을 소비했다. 이 기록은 운영 CNPG migration, T18 EC2/생산자 토큰·Tailscale ACL, 실제 HTTP 수신, T17 판정·LB 전환, 전체 COLLECT→DONE 인수의 증거가 아니다. 이 연결은 담당자 인계와 리뷰 이후 진행한다.
