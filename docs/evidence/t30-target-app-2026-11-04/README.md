# T30 목적지 앱 로컬 실측 (2026-11-04)

**렌더한 `demo-chat` Deployment와 Service는 임시 k3s의 restricted namespace에서 떴고, 코드 판정 32개를 모두 통과했다.**
업무 쓰기 1건은 풀러 뒤 출발지 대역 DB에만 들어갔고 목적지 DB는 0행, 목적지 `chat_app`은 NOLOGIN 그대로였다.
**실제 환경이 아니다.** RKE2, Tailscale, Edge, 게시된 이미지 바이트, 개발 VM의 QEMU에서는 재지 않았다.
클라우드 자원과 실제 클러스터는 건드리지 않았다.

숫자는 [`lab-result.json`](lab-result.json)에서 왔다. 소스는 플랫폼 `cbf8818`, demo-chat `8d82a92`(게시본과 같은 소스의 로컬 빌드)이다.

## 판단 세 가지

**1. 지금 이미지가 AWS 없이 뜨는가: 뜨지 않는다.** `ce5727e`의 제약이 게시본 `8d82a92`에도 그대로다. `app/main.py`의 lifespan이
`credentials.get()`으로 `app/db_secret.py`의 `GetSecretValue`를 호출하고, 실패하면 앱이 죽는다. 같은 이미지로 응답 지점을 없애
봤더니 `EndpointConnectionError`로 시작에 실패했다. 이 호출은 S2 시연을 위해 일부러 남긴 것이라 앱을 고치지 않았다.
[ADR-0016](../../adr/0016-target-app-secrets-manager-compat.md)대로 같은 Pod의 별도 컨테이너가 루프백에서 응답한다. **출구 증거가 아니다.**

**2. T9 없이 DB 접속 정보를 어떻게 주는가: T9 계약의 Secret 이름을 참조만 한다.** 렌더 결과에 Secret 객체와 값이 없다.
[T9 전달 실측](../2026-11-04-target-secret-delivery/README.md)을 보면 세 Secret이 개발 VM에 이미 ESO로 전달됐을 수 있어서, 적용 전에
있는지 먼저 본다. 없을 때의 수동 절차는 [운영 문서](../../../infra/envs/target-workloads/README.md#개발-측정용-앱)에 있고,
시험 클러스터에서 그 명령을 그대로 실행해 동작을 확인했다(`c1c74ab` 소스 회차). 값은 파일 · 인수 · 출력에 남지 않았다.

**3. `X-Bravo-Env`: `APP_ENV=onprem`으로 목적지 값이 나온다.** 200, 403, 503, 쓰기 차단을 포함해 시험한 앱 응답 전부에 `onprem` 하나만 붙었다.
이 헤더는 `c1c74ab`부터 있고, 게시본 `8d82a92`([digest `ca5ce1b2`](https://github.com/Softbank-Hackathon-Finals/demo-chat/actions/runs/41803312129))에 들어 있다.
그 전 게시본 세 개는 헤더가 없어 렌더러가 거부한다.

## 잰 것

| 상태 | 엔드포인트 | `/readyz` | `/api/health/db` | `GET /api/messages` | `POST /api/messages` | 행 |
| --- | ---: | ---: | --- | --- | --- | --- |
| 목적지 NOLOGIN, 직접 | 1 | 200 | 503 (2.0초) | 503, `Retry-After: 2` (5.0초) | 차단 중 503 (0.01초), 해제 뒤 503 (5.02초) | 없음 |
| 목적지 NOLOGIN, 풀러 경유 | 1 | 200 | 503 (2.0초) | 503, `Retry-After: 2` (5.02초) | 차단 중 503 (0.01초), 해제 뒤 503 (5.01초) | 없음 |
| 풀러 → 출발지 대역 | 1 | 200 | 200 | 200 | 차단 중 503, 해제 뒤 201, 같은 키 재전송 200 | 출발지 +1, 목적지 0 |

NOLOGIN에서 앱은 **Ready로 보인다.** `/readyz`가 DB를 보지 않기 때문이고, 컷오버 중 Pod가 빠지지 않게 하려는 demo-chat의 설계다.
실패는 503과 `Retry-After`로 드러나고, 원인은 앱 로그와 목적지 DB 로그의 `not permitted to log in`으로 확인했다.
비밀번호는 맞게 넣었으므로 역할 검사에서 막힌 것이다. 재시작 뒤 쓰기 차단은 다시 켜졌다. 컷오버 전 첨부 업로드는 403이다.

| 앱을 띄우면 안 되는 상태 | 결과 |
| --- | --- |
| `target-app-credentials` 없음 | Pod `Pending`, 앱 미시작, 엔드포인트 0 |
| 자격 파일 형식 오류 | 호환 컨테이너 재시작, 앱 미시작, 엔드포인트 0 |
| Secrets Manager 응답 지점 없음 (대조군) | 앱 시작 실패 후 재시작, 엔드포인트 0 |

단위 시험 35개, 변이 43개 모두 잡음(`scripts/tests/target_application_mutation_check.py`).
실제 SDK 호출은 demo-chat이 잠근 boto3 1.43.123으로 확인했다. 저장소 전체 시험은 로컬 Python 3.9에서 기존 오류 26건이 그대로 남고 새 오류는 없다.

## 시험에서 찾은 것

전체 시험은 6번 돌렸고 3 · 5 · 6회차가 통과했다. 1~5회차는 `c1c74ab` 소스, `lab-result.json`은 깨끗한 커밋에서 게시본 소스로 돌린 6회차다.
실패가 코드와 운영 규칙을 바꿨으므로 남긴다. 1회차는 판정 2개 실패로 시험 설계 문제와 호환 컨테이너 결함을 드러냈고, 2 · 4회차는 시간 초과였다.

**PgBouncer: 시작 후 첫 로그인이 틀리면 재시작 전까지 막힌다.** 4회차에서 `/api/health/db`가 240초 동안 열리지 않았다(2회차도 60초에 시간 초과). 4회차 풀러 로그는
`password is SCRAM secret but client authentication did not provide SCRAM keys`였다. PgBouncer 1.25.1은 `scram.c`에서 첫 시도 때
`adhoc_scram_secrets_cached`를 켜고, `client.c`는 그 뒤의 `adhoc` 로그인에서 SCRAM 키를 저장하지 않는다. 경로를 바꾸는 순간
옛 앱 Pod가 옛 비밀번호로 먼저 붙으면 이렇게 된다. 직접 재현한 결과는 아래와 같다.

| 풀러 재시작 뒤 순서 | 1번째 | 2번째 | 3번째 |
| --- | --- | --- | --- |
| 틀림 → 맞음 → 맞음 | 비밀번호 거부 | 서버 로그인 실패 (SCRAM 키 없음) | 서버 로그인 실패 (SCRAM 키 없음) |
| 맞음 → 틀림 → 맞음 | 성공 | 비밀번호 거부 | 성공 |

렌더 결과의 결함이 아니고 같은 이미지와 verifier 방식을 쓰는 출발지 풀러에도 해당될 수 있다. 운영 규칙을 문서에 적었고, 시험은 경로를 바꾸기 전에 앱을 먼저 멈춘다.
그렇게 바꾼 뒤 경로 전환을 8번 반복했더니 모두 0.2~0.3초에 DB 경로가 열렸다([`route-switch-repeat.jsonl`](route-switch-repeat.jsonl), 일회성 측정).

**롤아웃 직후 요청 실패.** 재시작 직후 Service가 사라지는 Pod로 요청을 보낸다. 종료 전 대기(preStop)를 넣었다
([`rollout-comparison.jsonl`](rollout-comparison.jsonl), 일회성 측정).

| | 재시작 | 직후 요청 | 실패 |
| --- | ---: | ---: | ---: |
| preStop 없음 | 10 | 120 | 54 (회당 3~8) |
| preStop 5초 | 10 | 120 | 0 |

**호환 컨테이너 결함 2건.** SIGTERM을 무시해 재시작마다 Pod가 유예 시간을 다 썼다(PID 1). Python으로 만든 시작 확인이
CPU 제한 아래서 1초를 넘겼다. 둘 다 고쳤고 시험을 넣었다. 수정 뒤 이전 Pod는 롤아웃 후 7.2초 만에 사라졌다.

**기각한 가설.** Secret을 바꾼 직후의 옛 값 마운트: 즉시 재시작 6회, 3초 뒤 재시작 4회 모두 최신 값이었다.
DB 경로 지연: 느린 회차에도 앱↔풀러 TCP는 이미 `ESTABLISHED`였다. 진단 6회차 하나는 다음 실험과 겹쳐 무효로 처리했다.

## 대역

| 실제 환경 | 이번 시험 |
| --- | --- |
| 개발 VM의 RKE2 1.35.9, amd64 QEMU | Docker 안 단일 노드 k3s `v1.35.5-k3s1`, arm64 |
| GHCR · ECR에 게시된 이미지 (amd64) | demo-chat `8d82a92` 소스의 로컬 arm64 빌드. 베이스 이미지 digest는 같고 이미지 digest는 다르다 |
| EKS의 출발지 DB | 같은 클러스터의 CNPG `source-db` (`source-workloads/render.py`, 저장소만 교체) |
| Tailscale egress | `source-db-rw`의 ClusterIP를 `pooler.py`에 전달 |
| OpenBao · ESO로 받은 Secret | 같은 이름의 Secret에 생성한 값 |
| CNPG Helm chart 0.29.1 | 공식 1.30.1 릴리스 매니페스트 (감시 범위 전체) |
| Edge · Tunnel | 없음. 같은 namespace의 Pod가 Service DNS로 호출 |

대역이 아닌 것: 렌더러 출력, demo-chat 소스, CNPG, PostgreSQL 18.4, PgBouncer 1.25.1(같은 digest), restricted Pod Security.

## 재지 않은 것

- 실제 Edge 경유, 실제 Tailscale 왕복, 부하. 지연 수치로 쓰지 않는다
- 게시된 이미지의 Pull과 시작, `export_target_image.py`의 새 이미지 실행(비공개 ECR 자격 필요), amd64 QEMU에서의 시작 시간. 넉넉한 startupProbe를 두었지만 재지 않았다
- 첨부 다운로드. SeaweedFS 고정 이미지가 amd64 전용이라 올리지 않았다. 복사 전 목적지에서 받는 첨부는 실패할 수 있다
- 자격 회전 중 앱 동작, T9를 나중에 적용할 때 ESO가 수동 Secret을 넘겨받는지
- 2회차의 시간 초과 원인. 그 회차 클러스터를 남기지 않아 로그를 못 봤다. 4회차와 같은 원인으로 보이지만 확인하지 못했다

## 재현

```sh
infra/envs/target-workloads/testing/app_lab.py --demo-chat-src <demo-chat 8d82a92 체크아웃> --output <결과 파일>
python3 -m unittest discover -s scripts/tests -p test_target_application.py
python3 scripts/tests/target_application_mutation_check.py
```

Docker Hub 로그인이 만료된 환경이라 베이스 이미지는 공개 미러에서 같은 digest로 받았다. 토큰은 건드리지 않았다.
