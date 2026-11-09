# NET-01.2 / NET-01.3 네트워크 관찰

`NetworkAccessProbe`는 T5의 외부→DB와 다른 namespace→DB 시험을 구현하는 서버 내부
관찰기다. **DB 5432와 Pooler 6432 두 대상을 함께 등록해야 한다.** 하나만 검사한 결과를
전체 시험의 PASS로 올리지 않는다. 외부 포트 스캔 NET-01.1은 이 구현에 포함하지 않는다.

## 실행 순서

각 포트마다 같은 대상 IP·포트를 고정하고 다음을 60초 이내에 수행한다. 전체 두 포트도
같은 60초 창을 벗어나면 실패한다. 연결 시한은 3초다.

1. 등록된 정책 설정과 실제 대상·시험 주체 결속을 새로 조회한다.
2. 허용된 앱 시험 주체에서 같은 DB/Pooler 접속을 확인한다.
3. 거부를 시험할 주체에서 허용된 대조 endpoint 접속을 확인한다.
   NET-01.3에서는 같은 시험 파드의 클러스터 DNS 조회도 필요하다.
4. 거부 주체에서 같은 DB/Pooler IP·포트의 TCP 접속을 시도한다.
5. 2·3의 대조군을 다시 확인한다.
6. 정책·자원 결속을 다시 조회하고 전후 설정 해시가 달라지면 결과를 수용하지 않는다.

| 관찰 | 결과 |
|---|---|
| 두 포트의 대조군 성공 + 연결 refused/timeout | deny |
| 대조군 성공 + 하나라도 connected | allow → T5 FAIL |
| 대조군 하나라도 실패 | UNKNOWN / POSITIVE_CONTROL_FAILED |
| 경로 없음·예상 외 전송 오류 | UNKNOWN / UNEXPECTED_RESPONSE |
| 전체 시험 시한 초과 | UNKNOWN / TIMEOUT |
| 정책 조회 권한 없음·등록 불일치·증거 저장 실패 | 완료 결과를 반환하지 않음; Runner에서 UNKNOWN |

**timeout만으로 deny를 만들지 않는다.** TCP connect 성공은 SQL 인증이나 TLS 검증이
아니다. 이는 별도 ENC/ISO 시험에서 판단한다. 접속 불가 관측은 특정 정책 하나가 원인임을
증명하지 않으며 정책 설정 해시는 원인 조사용 결속 자료다.

## 신뢰 경계와 배치

- `NetworkTarget`은 등록 서버가 선택한다. 요청자·앱·모델이 주소·포트·agent를 지정할 수 없다.
- raw DB와 Pooler의 실제 UID, endpoint, 허용/거부 agent의 Pod 또는 VM UID를 등록한다.
  허용 agent는 앱의 실제 네트워크 경로를 사용하고, 거부 agent는 다른 namespace 또는
  외부 호스트여야 한다. 문자열 label만 다르다고 다른 신원으로 인정하지 않는다.
- 원격 dispatcher가 서버 인증·실행 주체·namespace·UID를 확인하고 요청별 nonce를 전달한다.
  응답도 인증된 채널로 받는다. nonce는 재사용 방지 결속값이며 서명이나 인증 자체가 아니다.
- `read_policy(context, target)`는 등록된 환경에서 실제 SG/NetworkPolicy와 필요한 자원
  결속을 읽고 `PolicySnapshot`을 반환한다. 업로드 JSON을 그대로 반환하면 안 된다.
  전후 API 읽기는 원자적 스냅샷이 아니므로 실행 직전 재검증과 승인 창이 필요하다.
- `SocketTCPRunner`는 등록된 agent **안에서** 실행하며 IP·포트 allowlist 최대 4개만 사용한다.
  데이터를 보내거나 계정·SQL·비밀을 전달하지 않는다. 포트 스캔 도구가 아니다.
- 원격 실행 및 DNS 수집은 5초 이내에 실제 프로세스가 종료되도록 dispatcher가 강제한다.
  관찰기는 늦은 응답을 거부하지만 멈춘 callback을 자체 강제 종료하지 않는다.
- 동일한 UTC 시간 기준을 쓰며 시계가 어긋난 응답은 UNKNOWN이다. 증거에 시각·소요시간·
  포트별 결과·정책 해시·자원 결속 해시를 보존하고 원문 IP·비밀은 공개하지 않는다.

시험 파드에 클러스터 관리자, hostNetwork, hostPath, Docker 소켓, NET_ADMIN 권한은
필요하지 않다. 정책 읽기 권한은 제어면 수집기에 분리한다. 운영 경로 통합 전 T6/T30의
등록·CNI 강제·SG/NetworkPolicy 읽기·다른 namespace 실행을 실제로 검증해야 한다.

## 수행한 검증과 한계

[로컬 실제 시험](../../../docs/evidence/t14-network-controls-2026-11-03/README.md)에서 TCP
연결, 커널 SYN 드롭, 경로 개방, 대상/대조군 장애를 확인했다. 이는 Docker 시험 환경이며
**AWS 보안 그룹이나 EKS/RKE2 NetworkPolicy 자격을 통과한 결과가 아니다.**

통합 시험기의 `--firewall`은 시험 대상 컨테이너 한 개에만 root·NET_ADMIN을 부여해 자체
network namespace에서 패킷 드롭을 만든다. 시험 agent는 무권한이고 호스트 네트워크·소켓은
연결하지 않는다. 이 fault injector는 운영 앱/agent의 배포 템플릿이 아니다.

기본 CI의 socket 시험은 loopback만 사용한다. Docker 시험은 수동 선택이며 실행마다
고유 자원을 만들고 그 자원만 정리한다. 재현 시 Python 3.12+, 잠긴 control-runner 의존성,
필요 시 iptables가 든 로컬 이미지의 불변 SHA를 전달한다.

```sh
PYTHONPATH=src python tests/integration/verify_network_controls.py \
  --image sha256:<로컬_검증_이미지_ID> --firewall --output <새_비공개_결과_디렉터리>
```

## 실제 포트가 다른 환경

풀러가 6432 대신 5432 등 다른 listener를 쓰면 v1의 정상 대조 실패를 거부 성공으로
바꾸지 않는다. 실제 서비스 역할·listener를 고정하는 [v2 후보](registered-network.md)를
별도 제공한다. 기본 T5/T19 계약과 기존 계획을 자동 전환하지 않는다.
