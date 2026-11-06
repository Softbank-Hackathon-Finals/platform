# T30 리허설 Pod → SeaweedFS 8333 측정

2026-11-06 KST. **임시 k3s에서 렌더한 정책을 적용하니 T15 검증 Job과 T3 시험 Pod, 기존 운영 클라이언트만 8333에 닿았고 나머지는 막혔다.** 진짜 T3 게이트도 3회 모두 통과했다. 실제 VM·RKE2·SeaweedFS에는 적용하지 않았다.

결과는 [`results.json`](results.json), 측정 소스는 [`source-manifest.json`](source-manifest.json)이다. 측정은 커밋 `5c39348`의 깨끗한 작업 트리에서 돌렸다.

## 실제와 대역

| 구분 | 무엇 |
| --- | --- |
| 실제 | `objects.py`가 렌더한 Service·NetworkPolicy·Pod 라벨, T3 namespace 템플릿과 `bin/create-and-check`, T15 Job 렌더러의 Pod 라벨, T9 `target-openbao` 정책 렌더, k3s v1.35.5 집행기(Flannel + 내장 kube-router) |
| 대역 | k3s 안의 SeaweedFS·OpenBao 프로세스. 같은 라벨과 포트로 TCP만 받는 Python이다. 클라이언트도 실제 Job이 아니라 같은 라벨을 붙인 Pod다 |

k3s 기본 CNI는 정책을 실제로 집행했다. 정책이 없을 때 8333·8334 30칸 중 27칸이 열렸고(닫힌 3칸은 T3 송신 정책이 막은 다른 포트), 정책을 넣자 5칸만 남았다. DNS 오류와 exec 실패는 막힘으로 세지 않는다.

## 누가 닿았나 (렌더한 정책 그대로)

| namespace | Pod 라벨 | 8333 | 8334 |
| --- | --- | --- | --- |
| 리허설 라벨 | T15 Job | 열림 | 막힘 |
| 리허설 라벨 | T3 시험 Pod | 열림 | 막힘 |
| 리허설 라벨 | 라벨 없음 / T3 echo / `bravo-object-client` | 막힘 | 막힘 |
| T3 정책 전체 적용 | T15 Job · T3 시험 Pod | 열림 | 막힘 |
| T3 정책 전체 적용 | 라벨 없음 | 막힘 | 막힘 |
| `rehearsal-` 이름이지만 라벨 없음 | T15 Job · T3 시험 Pod | 막힘 | 막힘 |
| 다른 `part-of` 값 | T15 Job | 막힘 | 막힘 |
| `target-dev` | `bravo-object-client=true` | 열림 | 막힘 |
| `target-dev` | T15 Job 라벨 / T3 시험 Pod 라벨 / 라벨 없음 | 막힘 | 막힘 |

## 정책을 일부러 넓혔을 때

세 번 연달아 기대한 칸과 같아야 인정했다. 9개 상태 모두 맞았다.

| 변이 | 열린 칸 수 | 새로 열리거나 닫힌 곳 |
| --- | --- | --- |
| Pod 선택자 제거 | 9 | 리허설 namespace의 다른 Pod가 열림 |
| 두 선택자를 별도 peer로 분리 | 11 | 위 + `target-dev`의 T15·T3 라벨 Pod가 열림 |
| namespace 선택자를 전체로 | 10 | 라벨 없는 namespace, 다른 `part-of` namespace, `target-dev`의 T15·T3 라벨 Pod가 열림 |
| 포트 제한 제거 | 7 | 허용된 Pod의 8334가 열림. T3 정책이 있는 namespace는 송신에서 계속 막힘 |
| T3 시험 Pod 허용 제거 | 3 | T3 시험 Pod가 닫힘 |
| 리허설 정책 삭제 | 1 | 운영 클라이언트만 남음 |

## T3 게이트

| 조건 | 종료 코드 | 내용 |
| --- | --- | --- |
| 렌더한 정책 (3회) | 0, 0, 0 | 18개 시험. P4 SeaweedFS·OpenBao 통과. N3·N12는 대조 불가이고 필수 항목이 아니다 |
| T3 시험 Pod 허용 제거 | 2 | SeaweedFS P4 실패. Pod 라벨을 T15 Job 하나로만 좁히면 게이트가 Job 전에 멈춘다 |
| 렌더한 정책 + T9 `target-openbao` 정책 | 2 | OpenBao P4 실패. **이 PR로 풀리지 않는 다음 차단이다** |

N3은 k3s에 메타데이터 서버가 없어서, N12는 대역의 8334가 대조용 namespace에서도 막혀서 대조 불가다. 실제 SeaweedFS는 8333만 선언하므로 N12가 생기지 않는다.

## 한 번만 따로 잰 것

대역이 답하지 못하는 질문이라 2026-11-05에 한 번씩 쟀다. 그 뒤 `objects.py`의 정책은 바뀌지 않았다. 측정 스크립트는 저장소에 넣지 않았으므로, 아래 값은 저장소의 시험으로 다시 확인되지 않는다.

**새 Pod의 첫 연결.** k3s에서 T3 정책이 적용된 namespace에 Pod를 새로 띄우고, 시작하자마자 8333에 연결했다. T15 Job 라벨 10개, T3 시험 Pod 라벨 10개 모두 첫 시도가 `REFUSED`였고 재시도하면 1.72초 안에 열렸다. 정책은 맞지만 새 Pod 주소가 집행기에 반영되기 전이다. T15 Job은 `AWS_MAX_ATTEMPTS=1`이라 이 틈에 걸리면 대조군이 깨져 `control_unconfirmed`로 끝난다. `aws` CLI가 뜨는 시간이 이 틈보다 긴지는 재지 않았다.

**실제 SeaweedFS.** `objects.py`의 이미지와 인수(`-s3.iam=false` 포함)를 그대로 쓰고, amd64 에뮬레이션과 루프백, 무작위 키로 돌렸다.

| 시도 | `closed` | `open-demo` |
| --- | --- | --- |
| 익명으로 운영 버킷 읽기 | 거부 | **허용** |
| `rehearsal-object-admin`으로 운영 버킷 읽기·쓰기 | 거부 | 거부 |
| 정책 적용 전 익명으로 시험 버킷 읽기 | 거부 | 거부 |
| `rehearsal-object-admin`이 시험 버킷에 공개 정책 적용 | 허용 | 허용 |
| 공개 정책 뒤 익명·서명 읽기 | 둘 다 허용 | 둘 다 허용 |

T15 대조군(공개 정책에서 익명 읽기 허용)은 이 구성에서 성립한다. 다만 후보 정책이 익명 읽기를 실제로 막는지는 재지 않았다.

## 재지 않은 것

- 실제 VM·RKE2(Canal). k3s의 거부 모양(`REFUSED`)과 첫 연결 틈의 길이는 다를 수 있다.
- 실제 Pod 안에서 도는 T15 Job 전체 흐름. 위 k3s 측정의 클라이언트와 서버는 TCP 대역이고, 실제 SeaweedFS 확인은 k3s 밖에서 했다.
- 후보 버킷 정책의 거부 동작.

Pod 라벨은 권한 경계가 아니다. `rehearsal-exec`는 자기 namespace의 Pod에 라벨을 직접 붙일 수 있다.

## 재현

```sh
uv run --no-project --python 3.12 python infra/envs/target-workloads/testing/verify_rehearsal_object_access.py --out <폴더>
```

`t30rh-k3s` 컨테이너 하나를 만들고 끝나면 지운다. Docker와 `python:3.12-slim`, k3s 이미지가 필요하며 `busybox:1.36`은 k3s가 공개 미러에서 받는다.
