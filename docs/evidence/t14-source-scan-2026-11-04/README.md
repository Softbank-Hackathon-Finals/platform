# T14 실제 출발지 소스·배포 설정의 비밀 탐지

2026-11-04 19:52 KST에 기존 `GitleaksProbe`와 `Runner`로 **SEC-01.2 한 항목**을
실행했다. 실제 앱과 대조한 소스 및 실제 Kubernetes 배포 설정의 검사다.
출발지 전체 통제, 목적지, T19 게이트 또는 컷오버의 통과를 뜻하지 않는다.

| 확인 항목 | 결과 |
|---|---|
| 배포 이미지의 소스 revision | `66e7419f404f4ec4addc71014aabfcdd44f34688` |
| 이미지 index digest | `sha256:4562bbf761d761cafc89f9db9153eb74550eb23972ea4f9080e93681d9d90d1d` |
| 실행 중 앱 파일과 해당 commit의 SHA-256 대조 | 14개 일치 |
| 해당 commit의 추적된 소스 스냅샷 | UTF-8 파일 66개, 바이너리 생략 없음 |
| 실제 배포 설정 스냅샷 | Deployment·Pod, 참조 ConfigMap 0개 |
| 고정 Gitleaks 8.30.1의 소스/설정 검사 | 탐지 0개 / 0개 → SEC-01.2 PASS |
| 이번에 실행하지 않은 full 프로필 시험 | 19개 UNKNOWN / NOT_RUN |
| 전후 대상 대조 | Deployment UID·spec·generation, Pod UID·imageID 동일 |

## 범위와 실행 방법

1. 운영자 읽기 권한으로 Deployment → ReplicaSet → Pod 소유 관계, 컨테이너 이미지,
   실제 imageID를 확인했다. imageID는 확인한 index 또는 그 index의 amd64 manifest로 한정했다.
2. 앱에서 Python 표준 라이브러리만 실행해 14개 앱 파일의 해시를 읽었다. 앱 코드를 import하거나
   업무 API, DB 쓰기, 환경변수·Secret 값을 읽지 않았다. 각 바이트를 원래 소스 commit과 비교했다.
3. 해당 commit의 소스 파일과 실제 Deployment/Pod의 설정을 별도 비공개 스냅샷으로 준비했다.
   실행 중인 이미지 레이어, 마운트된 파일, runtime Secret 값, 전체 Git 이력은 검사 범위 밖이다.
4. 운영자 장치에서 고정된 scanner를 실행했다. 실행 파일의 배포 archive·공식 checksum·바이너리
   해시를 확인했으며, 제품은 기본 탐지 규칙 전체를 사용하고 앱의 config·allowlist·허용 주석을
   신뢰하지 않는다. scanner 자식 프로세스에는 운영자의 자격 증명을 상속하지 않는다.
5. 제품이 만든 증거를 접근 제한 로컬 디렉터리에 기록하고 파일·디렉터리를 fsync했다.
   원문 findings를 공개하거나 모델에 보내지 않았고, 증거에는 개수·해시·범위·시각만 기록했다.

`control-run.json`과 `proof-0.json`은 민감 식별자를 포함하지 않는 제품 출력의 동일 바이트다.
`output_sha256`은 proof 바이트와 일치한다. `source-manifest.json`은 실제 실행한 제품 소스와
동일한 Git blob의 해시이며, `app-source-manifest.json`은 검사한 앱 revision의 파일 목록이다.
운영 리소스 원문과 실행 harness는 비공개로 보존하고 `summary.json`에 해시만 남겼다.
해시 자체가 외부 독자에게 실행 사실이나 리소스 실체를 독립 증명하는 것은 아니다.

## 실패 기록과 남은 검증

- 첫 준비 시도는 imageID를 amd64 manifest만으로 예상했다. 실제 containerd가 index digest를
  보고해 사전 결속 확인에서 중단했다. 앱 파일 읽기·scanner 실행 전이므로 PASS로 세지 않았다.
  두 번째 시도에서 index와 그 안의 amd64 digest만 수용하고, 별도로 앱 파일 해시를 대조했다.
- 운영 앱에 가짜 비밀을 넣는 변경은 하지 않았다. 탐지·도구 실패·범위 거부의 대조 사례는
  기존 제품 회귀 시험의 근거이며, 이번 실환경에서 재현한 사례로 합치지 않는다.
- 0개 탐지는 명시한 규칙과 스냅샷 범위에서의 결과다. 모든 형태의 비밀정보 부재를 보장하지 않는다.
- `aws:eks/source`는 이 운영자 자격 시험의 범위 표기다. T10의 등록·현재 계획·승인 권한으로
  대신 사용하지 않는다. 중앙 증거 저장소와 목적지 실행은 별도 인수 대상이다.
- 최신 게시 이미지의 ECR 전달은 별도 작업이다. 아직 배포하지 않은 최신 이미지로 이번 검사의
  revision이나 해시를 바꾸지 않는다.

이 변경은 SEC-01.2의 새 증빙만 추가한다. 별도 IAM-01.1 증빙 PR #209의 변경이나 병합을
대신하지 않는다. 관련 태스크: [T14 #21](https://github.com/Softbank-Hackathon-Finals/platform/issues/21).
