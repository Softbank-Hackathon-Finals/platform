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

**첫 실행 실패:** 시험 스크립트가 Collector 로그를 읽는 줄에서 `capture_output`과 `stderr`를 함께 넘겨 예외로 끝났다(저장소 성질과 무관). 고친 뒤 새 출력 경로로 다시 실행했다. 두 실행 모두 만든 컨테이너 · 네트워크 · 시험 이미지를 지웠다.
