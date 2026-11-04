# T14 네트워크 관찰 검증 — 2026-11-03

DB 5432와 Pooler 6432를 같은 시험에 묶어 연결 여부와 대조군 상태를 확인했다.
실제 Docker 내부망과 대상 컨테이너의 iptables SYN DROP을 사용했다.
AWS SG 또는 EKS/RKE2 NetworkPolicy의 통합 검증 결과는 아니다.

| 실제 조건 | 두 포트 통합 결과 |
|---|---|
| 거부 주체 SYN DROP, 허용 주체·대조 endpoint·DNS 전후 정상 | deny |
| DROP 제거 후 연결 성공 | allow (T5 기대 deny와 달라 FAIL) |
| 허용 주체의 대상 연결도 거절 | UNKNOWN / POSITIVE_CONTROL_FAILED |
| 거부 주체의 대조 endpoint 중단 | UNKNOWN / POSITIVE_CONTROL_FAILED |

최종 실행은 12개 비공개 증거를 남겼다. 공개 요약·소스 SHA·불변 시험 이미지 ID는
`results.json`에 있다. 원문 endpoint·컨테이너 UID·실행별 증거는 비공개로 보존했다.
TCP 연결 관찰은 SQL 인증·TLS·정책 원인을 입증하지 않는다.

## 재현과 실패 기록

- 시험기: `services/control-runner/tests/integration/verify_network_controls.py`.
- `--firewall`은 대상 fixture 한 개에만 자체 network namespace의 NET_ADMIN을 준다.
  시험 agent는 비root·capability 제거·읽기 전용이며 호스트 네트워크/소켓을 연결하지 않는다.
- 앞선 별도 bridge 시험은 timeout이 아닌 경로 오류를 반환했다. 이를 deny로 간주하지 않고
  UNKNOWN으로 유지했다. 재현 가능한 패킷 드롭을 위해 전용 iptables fixture를 추가했다.
- 대상 프로세스를 종료한 초기 실험에서는 정책 조회도 불가능해 완료 결과를 만들지 않았다.
  최종 장애 시험은 정책 읽기를 유지하면서 허용 연결을 거절해 대조군 실패를 검증했다.
- 최종 두 포트 구현의 단위 시험 52개와 전체 control-runner 393개·subtest 58개 통과.
  실제 gitleaks 실행과 loopback socket 시험도 포함했다.

## 남은 통합

T6/T30 등록 자원·신뢰된 agent dispatcher·정책 읽기 권한·실제 CNI/SG를 연결해야 한다.
등록 문자열이나 업로드된 정책 JSON만으로 인증된 시험 주체임을 주장할 수 없다.
NET-01.1 외부 노출 스캔과 실제 두 환경 비교의 완료도 이 결과에 포함하지 않는다.
