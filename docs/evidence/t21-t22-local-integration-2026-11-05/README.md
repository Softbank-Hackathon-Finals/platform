# T21·T22 최신 코드의 로컬 통합 시연

11월 5일 실제 로컬 앱 두 개·PostgreSQL·OTel Collector·관측 HTTP API·T10/T21 콘솔을
연결했다. [23개 통합 검사](summary.json)는 모두 통과했다. 원본 결과·부하 원장·
Chromium/WebKit 스크린샷은 비공개 보존하며 원본 파일 해시를 공개 요약에 남긴다.

| 장면 | 실제 확인 |
|---|---|
| 두 앱의 업무 요청 | 앱별 60초, 쓰기 3,000 + 읽기 9,000 요청. HTTP 오류·연결 오류·timeout 0 |
| 데이터 확인 | 각 실행의 쓰기 3,000건을 실제 DB에서 재대조. 유실·중복·내용 충돌 0 |
| 관측 | 실제 HTTP 히스토그램과 환경 헤더, mTLS 수집, 환경별 상태 조회 |
| 창 전달 | 각 10초 창 6개. 누락·중복·전달 실패 없음, 잘못된 토큰·창 재전송은 거부 |
| 콘솔 | Chromium/WebKit 실제 관측 표시. 별도 환경 조회 권한 검사, 로그인 회수 시 표시 제거 |
| WebSocket 전환 | 쓰기 차단·Retry-After, 관리 API 1012 종료, 다른 앱 hello, cursor 재접속, 같은 키 재시도·DB 한 행 유지 |
| 수집기 장애 | 목적지 Collector를 정지하면 미확인으로 표시. 없는 값을 0으로 만들지 않고 출발지 관측은 유지 |

첫 실행은 새 작업 폴더의 관측 실행 환경이 없어 앱 시험 전에 중단됐다. 잠긴 의존성을
준비한 뒤 별도 결과 폴더에서 재실행했다. 실패·정리 기록은 보존했으며 성공 결과로 덮지 않았다.
재실행 후 Compose·시험 이름 범위의 컨테이너 잔류 0건을 읽기 전용으로 확인했다.

## 재현

고정한 [소스 목록](source-manifest.json)의 앱과 플랫폼 패키지를 준비한다.
T18·T36/T37·T21의 잠긴 실행 의존성, 앱 시험 이미지, Playwright Chromium/WebKit과
드라이버의 WebSocket 의존성 16.1.1이 필요하다. 원격 Docker context는 거부한다.

```sh
python testing/observability_e2e.py \
  --loadgen /checkout/platform/services/loadgen \
  --observability /checkout/platform/services/observability \
  --windows /checkout/platform/services/observability \
  --console /checkout/platform/services/console --console-browser \
  --duration-per-environment 60 --output /private-output/new-integration
```

위 스크립트는 demo-chat 저장소에 있다. 현재 앱/콘솔의 시험 대상 SHA를 요약에서 확인한다.
앱의 pipeline 후보는 아직 별도 Draft이며 실제 배포본으로 간주하지 않는다.
운영 AWS·모델·GitHub 자격은 시험 프로세스에서 제거하고 AWS API는 로컬 Moto를 사용한다.

## 운영 인수와의 구분

- 두 앱은 **같은 Mac의 한 PostgreSQL**에 연결한다. AWS·onprem은 이 시험의 환경 이름이다.
- Nginx는 로컬 진입점 대역이며 실제 Cloudflare/Tailscale 경로가 아니다.
- WebSocket 전환은 실제 앱/프록시 전환이다. 별도 DB의 데이터 이전·writer 전환을 증명하지 않는다.
- 로그인·계획·권한은 명시적인 로컬 fixture다. 운영 SSO·T10 등록·실제 승인 인수를 대신하지 않는다.
- 20/40ms 반복 성능 자격의 기존 실패는 해결됐다고 표시하지 않는다. 이번 경로는 그 실험과 다르다.
- T14 양쪽 통제, T19 운영 게이트, 실제 AWS→온프렘 이전·무중단·T21/T22 전체 완료는 남는다.
