# T34 OTel 전달 자격 시험 — 2026-11-03

고정 OTel Collector 이미지 두 개를 격리된 Docker 내부 네트워크에서 실행했다. 합성 CNPG CRI 로그와 K8s audit JSON을 사용했으며 호스트 포트·AWS·팀 EKS/RKE2는 사용하지 않았다. 결과는 [results.json](results.json)에 있다.

- mTLS 전달, SQL·요청 본문 제거, 등록 Pod UID 경로만 수집, 수집기 등록 출처 덮어쓰기 확인
- 수신기 중단 중 6개 기록 추가 → 송신기 SIGKILL → 같은 영속 checkpoint/queue로 재시작 → 모든 표식 수신
- 잘못된 서버 CA의 실제 인증서 검증 오류와 전송 보류 → 정상 CA 복구 후 대기 기록 수신
- 다른 CA가 발급한 클라이언트 인증서를 수신기가 거부 → 정상 인증서 복구 후 대기 기록 수신
- 로그 rename 회전 후 새 파일 수집, 총 합성 기록 11개 확인

첫 시도는 정상 전달·중단/복구를 확인했고, 두 번째 CA 부정 시험은 production error 로그 수준에서 재시도 진단을 볼 수 없어 **검증 실패**로 남겼다. 합성 자료만 사용하는 독립 시험에서 진단 수준을 info로 바꿔 TLS 오류를 확인했고, 이후 클라이언트 issuer 거부까지 포함해 통과했다. 운영 기본 구성은 error 수준을 유지한다. raw 진단·인증서·키는 비공개 실험 경로에 보존하며 여기에는 넣지 않았다.

코드는 실행 뒤 커밋했다. `implementation_commit`은 동일한 파일 bytes를 담은 후속 커밋이며 실행 전 커밋 고정을 주장하지 않는다. 결과의 파일 hash로 실행 소스와 연결한다. 단위 시험 28개와 기존 control-runner 회귀를 별도 수행했다.

파일 exporter는 검증용 수신기다. 이 결과는 실제 환경의 로그인·DDL·RBAC 발생, CloudWatch/RKE2 중앙 검색, 검색 역할 거부, 보존·불변 감사, 부하·디스크 고갈·장기 운용 완료가 아니다. 실제 양쪽 수집 경로와 T14 검색 어댑터 연결 전까지 T34 전체 완료로 표시하지 않는다.

## 실제 노드 파일 수집 후속 검증

작업 전용 kind의 실제 CNPG Pod 로그와 kube-apiserver audit 디렉터리를 읽기 전용으로 연결했다. 로그를 복사·재생하지 않고 filelog → 환경 내부 mTLS 수신기 → #124의 등록 출처 검증 → 감사 검색으로 연결했다. 관리자 로그인·DDL·RoleBinding 변경 세 종류가 모두 `present`였으며 마지막 실측은 1.826초였다. 결과는 [live-node-collection.json](live-node-collection.json)에 있다.

첫 실측에서는 CNPG의 일반 기동 로그에 `record`가 없으면 변환기가 nil에 `delete_key`를 실행해 배치가 막히는 결함을 발견했다. `record`/`audit`가 map일 때만 처리하고 잘못된 형식은 제거하도록 수정했다. 합성 기동 로그·문자열/null record·문자열 audit를 실제 Collector에 섞고 후속 canary 전달과 민감 문자열 제거를 확인했다. 중단·SIGKILL·TLS 거부·회전 재검증도 통과했다([mixed-log-regression.json](mixed-log-regression.json)). 첫 실패 로그는 비공개로 보존했다. 오류를 무시하도록 error_mode를 낮추지는 않았다.

검색용 10분 ServiceAccount 토큰은 수신 Pod 하나의 `get pods`, `get pods/log`만 가진다. 해당 경로로 가져온 결과에서도 세 canary 종류를 다시 검증했다. Secret 목록, Pod 목록, 다른 Pod와 로그, 원본 DB 로그, 삭제 dry-run, Role 생성 dry-run 7종은 모두 HTTP 403이었다. RoleBinding 회수 후 로그 조회 403, 복구 후 200과 동일 Pod UID를 확인했다([live-reader-rbac.json](live-reader-rbac.json)). 토큰·키는 공개 결과에 포함하지 않는다.

한계와 재현 조건:

- 수신기의 file exporter와 로그 출력 보조 컨테이너는 자격 시험 fixture다. 실제 검색 서비스·장기 보존·불변 감사 저장소가 아니다.
- 검색 fixture에 과거 로그가 있어 최초 900 KB 제한 조회에서 표식이 잘렸다. 이를 통과시키지 않았고, 16 MB로 명시한 제한 안에서 약 9 MB를 읽어 세 종류를 재검증했다. 운영 검색에는 표식/시간 인덱스·페이지 완전성 검증이 필요하다. 작은 제한을 전역으로 완화한 제품 변경은 없다.
- 노드 파일 basename만 kind의 `kube-apiserver-audit.log*`로 맞췄다. 실제 RKE2 경로 자격 시험은 남아 있다.
- 송신기는 API 토큰 없이 해당 CNPG Pod UID 디렉터리와 노드 감사 디렉터리만 읽기 전용으로 마운트했다. 노드 소유 파일 읽기를 위한 root UID는 쓰지만 capability 전부 제거·권한 상승 금지·읽기 전용 rootfs로 실행했다. 비root 권한 배치나 호스트 침해 방어까지 검증한 것은 아니다.
- 이번 Kubernetes 시험의 queue는 emptyDir이므로 Pod 교체 내구성을 주장하지 않는다. 별도 Docker 시험만 동일 영속 경로에서 SIGKILL 복구를 검증했다.
- 실제 실행한 모듈의 해시는 각 결과에 포함한다. #124 디코더는 별도 PR의 코드이므로 이 PR 단독으로 운영 검색 통합이 완료됐다고 볼 수 없다.
