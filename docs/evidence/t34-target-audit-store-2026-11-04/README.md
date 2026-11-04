# T34 목적지 감사 저장소 실측 — 2026-11-04

[ADR-0016](../../adr/0016-target-audit-store.md)의 근거다. 목적지 감사 기록을 SeaweedFS Object Lock 버킷에 OTLP JSON 객체로 보존할 때, 권한 분리와 보존 잠금이 이 버전에서 실제로 동작하는지 쟀다. 결과와 이미지 · 소스 해시는 [`results.json`](results.json)에 있다.

**로컬 Docker 시험이다.** 외부 연결 없는 내부 네트워크, 공개 포트 없음, 클라우드 자격 없음. 키와 CA는 실행마다 새로 만들고 비공개 출력에만 두며 끝나면 키 파일을 지운다. 실제 목적지 RKE2 · T9 SeaweedFS 배포에서 잰 결과가 아니다.

| 구성 | 값 |
|---|---|
| 저장소 | SeaweedFS 4.52 (T30 `objects.py`와 같은 digest), S3 HTTPS(사설 CA), Iceberg · Lance 끔 |
| 전달기 | OTel Collector contrib 0.164.0 (T34와 같은 digest), `awss3` exporter, `otlp_json`, uuidv7 키 |
| 검사 클라이언트 | python 3.12-slim(digest) + boto3 1.40.70 |
| 컨테이너 | 읽기 전용 루트, capability 제거, no-new-privileges, 비루트 |

| 확인 | 결과 |
|---|---|
| Object Lock 버킷, COMPLIANCE 기본 보존, 버전 관리 Enabled | 통과 |
| 쓰기 identity: PUT 성공, 같은 키 조건부 PUT 412 | 통과 |
| 쓰기 identity: 삭제 · 버전 삭제 · 버전 중지 · 정책 삭제 · 읽기 · 목록 403 | 통과 |
| 검색 identity: 목록 · 버전 목록 · 버전 읽기 성공, PUT · 삭제 403 | 통과 |
| 관리 identity: 잠긴 버전 삭제 403 | 통과 |
| 익명 목록 403 | 통과 |
| Collector → HTTPS: OTLP JSON 객체 1개 · 기록 1건, COMPLIANCE 적용, 관리자 버전 삭제 403, 오류 로그 없음 | 통과 |
| LastModified가 PUT 시각을 1초 안에서 감쌈 | 통과 |
| 함정 재현: 조건 없는 덮어쓰기 → 버전 추가, 관리자 삭제 표시 → 일반 목록에서 숨김, 원본 버전 그대로 | 재현됨 |

두 함정 때문에 검색 어댑터는 버전 목록으로 읽고, 한 키의 버전이 여럿이거나 삭제 표시가 있으면 UNKNOWN으로 다뤄야 한다(ADR-0016 3절).

**재현**

```sh
cd services/control-runner
PYTHONPATH=src python tests/integration/verify_target_audit_store.py --private-output <새_비공개_경로>
```

**실행 기록:** 첫 실행은 시험 스크립트가 Collector 로그를 읽는 줄에서 `capture_output`과 `stderr`를 함께 넘겨 예외로 끝났다(저장소 성질과 무관). 고친 뒤 두 번째 실행이 통과했다. k3s 시제품을 위해 스크립트(저장소 주소 환경 변수 · 검색 단계 · 첫 연결 대기)를 바꾼 뒤 세 번째, 공개 증빙 검사에 맞춰 버킷 ARN 표기만 바꾼 뒤 네 번째로 다시 실행했다. 두 실행 모두 결과가 두 번째와 항목 · 값이 같다. `results.json`은 네 번째 실행 결과와 지금 스크립트 해시다. 모든 실행이 만든 컨테이너 · 네트워크 · 시험 이미지를 지웠다.

## k3s 시제품

[`templates/target-audit-store`](../../../services/control-runner/templates/target-audit-store/)를 Docker 안 k3s v1.35.9(목적지 RKE2 v1.35.9와 같은 쿠버네티스)에 그대로 적용했다. 목적지에 적용할 출발점이고 운영 배포가 아니다(저장소 데이터는 emptyDir).

| 확인 | 결과 |
|---|---|
| 저장소(T30과 같은 SeaweedFS 인자 + S3 TLS) · 전달기가 restricted 네임스페이스에서 준비됨 | 통과 |
| 관리 역할 Pod가 Object Lock 버킷 · COMPLIANCE 보존 · 버킷 정책 생성(`-s3.iam=false`에서도 정책 적용) | 통과 |
| source 역할 Pod의 OTLP 감사 기록 → 전달기 → HTTPS → 저장소 | 통과 |
| 검색 역할 Pod: 검색 키만으로 버전 목록을 끝까지 읽음, 객체 1 · 기록 1, 변조 흔적 없음, PUT 거부 | 통과 |
| 쓰기 · 검색 · 관리 · 익명 권한과 두 함정이 Docker 실측과 같음 | 통과 |
| 역할 라벨 없는 Pod(같은 네임스페이스) · 다른 네임스페이스 Pod → 저장소 8333 연결 안 됨 | 통과 |

```sh
bash services/control-runner/tests/integration/verify_target_audit_store_k8s.sh
```

**k3s 첫 실행 실패:** 관리 역할 Pod의 첫 연결이 `EndpointConnectionError`로 끝나 버킷이 만들어지지 않았고, 그 때문에 검색 · 권한 검사도 이어서 실패했다(3건). 새 Pod의 첫 연결이 NetworkPolicy 반영 전에 거부되는 k3s(kube-router) 현상으로, #199에서도 본 것이다. 검사 스크립트가 연결될 때까지 최대 60초 기다리게 고친 뒤 다시 실행해 6/6 통과했다. 주석과 버킷 ARN 표기를 고친 커밋본으로 두 번 더 실행해 모두 6/6 통과했고, `results.json`의 해시는 마지막 커밋본이다. 모든 실행이 끝나고 클러스터 · 시험 이미지 · 키를 지웠다.
