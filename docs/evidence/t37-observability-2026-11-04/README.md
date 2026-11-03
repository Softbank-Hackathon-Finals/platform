# T37 관측 경로 로컬 검증 — 2026-11-04

T37 / platform#104의 로컬 검증 기록이다. 기준 커밋, 검사한 소스의 SHA-256, 이미지 digest, 결과는 [`results.json`](results.json)에 있다. **로컬 Docker와 Docker 안 k3s에서 한 시험**이다. 실제 출발지(EKS) · 목적지(RKE2) · 플랫폼(RKE2) 배포나 클라우드 자원 변경은 하지 않았다.

| 시험 | 결과 | 확인한 것 |
|---|---|---|
| `pytest` | 55 통과 | 판정 계약 사례 25 · 거부 8, 창 요약 생성, 조회 API(인가 · 고정 조회식 · 503 · DB 뷰), 런북 링크 |
| `promtool` | 규칙 8, 단위 시험 통과 | 관측 경로 알림 4, DB 알림 4 |
| `testing/run.sh` | 17/17 | mTLS 거부 3종, 위조 · 허용 목록 · 카나리, 디스크 큐, 생존 신호 사칭, 알림, 조회 API(OTel HTTP 히스토그램 → 요청률 · 오류율 · p95) |
| `testing/k8s.sh` | 14/14 | Pod 보안 표준 restricted, NetworkPolicy(대조군 포함), 클러스터 안 mTLS, 조회 API, T6과 같은 CNPG 지표와 최소 권한, 알림 |
| `testing/rotation.sh` | 14/14 | 재시작 없는 leaf 교체, 서버 인증서 교체, CA 묶음 교체(Collector 계획된 재시작 1회) |
| `trivy config` | HIGH · CRITICAL 0 | 중간 등급 KSV-0125(신뢰 레지스트리)는 클러스터 승인 정책 몫 |

자원 실측(데모 예상의 약 10배 부하, 3분):

| 컨테이너 | 최대 메모리 | 평균 / 최대 CPU |
|---|---|---|
| Collector | 107MiB | 33m / 92m |
| Prometheus | 83MiB | 15m / 42m |
| 조회 API | 50MiB | 4m / 16m |

이 값으로 매니페스트의 request · limit을 정했다. 시계열 종류 수가 적었고 Docker Desktop VM이라 EC2와 CPU 특성이 다르므로, 실제 환경에서 다시 잰다.

시험이 찾아 고친 결함 8건은 `results.json`의 `defects_found_by_tests`에 있다. 운영에 영향이 큰 두 가지는 다음과 같다.
- **Collector 0.164.0 keep-alive 결함:** 기능 게이트 `pkg.confighttp.PrioritizeNewKeepalive`(Beta, 기본 켜짐) 경로에서 `disable_keep_alives`를 무시한다. 그래서 인증서 교체와 신뢰 제거가 기존 연결에 반영되지 않았다. 게이트를 꺼서 해결했다.
- **Collector의 서버 검증 CA 목록:** 시작할 때만 읽는다. 그래서 CA 묶음 배포 단계에서 Collector를 재시작하도록 절차에 넣었다([인증서 절차](../../observability/certificates.md)).

원본 시험 로그는 k3s Pod 주소 등 공개 증빙 기준(개별 호스트 IPv4)에 걸리는 값을 담고 있어 저장소에 넣지 않았다. 로그에서 판정과 측정값만 옮겼다.
