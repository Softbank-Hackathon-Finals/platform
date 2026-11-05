# T21·T22 1시간 로컬 통합 연속 시험

11월 5일 **20:58–21:58 KST**에 출발지·목적지 앱을 각각 30분씩 순차 실행했다.
실제 로컬 PostgreSQL, 앱 두 개, mTLS OTel Collector, Prometheus, T36 HTTP 수신,
T21 콘솔과 Chromium/WebKit을 연결한 **23개 검사가 모두 통과**했다.

| 구간 | 쓰기 / 읽기 | HTTP·연결·timeout 실패 | DB 유실·중복·내용 충돌 | 10초 관측 창 |
|---|---:|---:|---:|---:|
| 출발지 30분 | 90,000 / 270,000 | 0 | 0 | 180개, 누락·전달 실패 0 |
| 목적지 30분 | 90,000 / 270,000 | 0 | 0 | 180개, 누락·전달 실패 0 |

요청 목표는 구간마다 쓰기 50/s·읽기 150/s이며, 관측한 실제 처리량도 각각 일치했다.
최종 DB에서 각 실행의 90,000개 확인 쓰기를 대조했다. 쓰기 p95는 각각 5.969ms·6.189ms,
읽기 p95는 4.174ms·4.264ms였다. 이는 **이 로컬 시험 조건에서의 수치**다.

마지막 화면만 보고 앞선 실패를 지우지 않도록 전체 요청 원장·창 보존·생성기 상태를
함께 확인했다. 환경 헤더 누락·관측 미귀속·부분 창·중복 창이 없었으며, 잘못된 수신 토큰과
창 재전송은 거부했다. 수집기를 정지하면 목적지 값은 UNKNOWN/null로 바뀌고 출발지 관측은 유지됐다.
콘솔의 무인증/다른 환경 조회·권한 회수도 거부했다.

WebSocket 시험은 실제 관리 API의 쓰기 차단과 1012 종료, 다른 앱 재접속·환경 hello,
마지막 메시지 이후 조회·동일 멱등 키 재전송·DB 한 행 유지를 확인했다. 인증 없는 종료와
쓰기 차단 전 종료는 거부했다. 시험 후 자체 Compose/컨테이너 정리와 잔류 0건을 확인했다.

## 소스와 재현

[요약](summary.json)에 원본 비공개 결과·창의 해시와 고정 실행 SHA를 기록했다.
공통 실행 파일 164개는 [앞선 고정 소스 목록](https://github.com/Softbank-Hackathon-Finals/platform/blob/02fd5a2fa4d59596c2fdf9c2814a040e55c3fdc8/docs/evidence/t21-t22-local-integration-2026-11-05/source-manifest.json)의
각 SHA Git blob 및 실제 실행 파일 바이트와 다시 대조해 모두 일치했다.

앞선 [짧은 통합 시험](../t21-t22-local-integration-2026-11-05/README.md)과 같은 구성에서
`--duration-per-environment 1800`을 사용한다. 모델·AWS·GitHub 자격은 시험 환경에서 제거하고,
Moto가 AWS API를 대체한다. 실제 Collector/Prometheus·앱·DB와 fixture 신원/계획을 구분한다.

## 자격의 한계

- 같은 Mac의 앱 두 개와 **공유 DB 한 개**다. AWS/onprem은 이 시험의 환경 이름이다.
- 직접 HTTP 부하 경로다. 원 보고서의 `NOT_EDGE_MEASURED`, `sli_usable_for_judgement=false`를
  유지한다. 실제 Cloudflare·Tailscale·WAN/TLS·DB 이전·writer 전환의 검증이 아니다.
- 짧은 코드 회귀와 호스트를 공유했다. 격리된 운영 성능 측정이나 환경 간 우열을 주장하지 않는다.
- 기존 20/40ms 반복 성능 시험의 실패를 해결한 것으로 표시하지 않는다. 앱 pipeline PR도 별도 Draft다.
- T14 양쪽 통제·T19 운영 계획·실제 컷오버와 휴대폰 참여 인수가 남아 있어 T21/T22 전체 Done은 아니다.

성공한 연속 시험은 로컬 연결의 안정성 근거이며, 실제 AWS→온프렘 무중단 이전의 증거로 바꾸지 않는다.
