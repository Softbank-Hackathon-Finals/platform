# T30 tailnet 경로 실제 측정

2026-11-06 KST에 두 번 쟀다. **03:51 실행에서 세 경로 모두 `PASS`였고, 셋 다 DERP(Tailscale 중계 서버) `tok`을 지났다.** 03:08 실행은 목적지 쪽 두 경로만 재서 전체 결과가 `UNKNOWN`이었다.

해석과 실행 방법은 [운영 문서](../../operations/t30-tailnet-path-check.md)에 있다.

| 파일 | 내용 |
| --- | --- |
| `result-both-sides.json` | 03:51 양쪽 실행의 도구 출력 그대로. 가릴 값이 없어 비공개 원본과 바이트가 같다 |
| `source-manifest-both-sides.json` | 03:51 실행의 커밋 `2f4dd10` 파일 해시. 도구와 `network.py`의 바이트는 03:08 실행과 같다 |
| `result.json` | 03:08 목적지 쪽 실행의 도구 출력 그대로. 가릴 값이 없어 비공개 원본과 바이트가 같다 |
| `supplement.json` | 03:08 실행 뒤에 따로 읽은 `tailscale status`와 `tailscale netcheck`. 정한 필드만 남겼고 원본 JSON은 저장하지 않아 대조할 원본이 없다. 판정이 아니다 |
| `source-manifest.json` | 03:08 실행의 커밋 `b2a5b50` 파일 해시 |

두 실행 모두 도구는 프록시 Pod 안에서 읽기 명령과 TCP 연결만 했다. 설정, ACL, 프록시는 바꾸지 않았다.

## 03:51 양쪽 실행

`KUBECONFIG`에 출발지와 목적지 kubeconfig를 이어 주고 `--source-context`와 `--target-context`를 함께 줬다. 출발지 EKS API는 사설 전용이어서, SSO로 로그인한 뒤 사설 플랫폼 호스트를 거치는 SSM 원격 포트 포워딩으로 붙었다. TLS는 원래 API 서버 이름으로 검증했다. 호스트 ID, 주소, 계정은 남기지 않는다.

| 경로 | 판정 | 연결 방식 | Tailscale ping 왕복 (최소/평균/최대) | ICMP 왕복 | MTU / DF 최대 |
| --- | --- | --- | --- | --- | --- |
| `target-replication-to-source-db` | `PASS` | 중계 `tok`, 응답 3건 모두 중계 | 67 / 68.3 / 70 ms | 68.7 ms | 1280 / 1280 (IPv6) |
| `target-pooler-to-source-db` | `PASS` | 중계 `tok`, 응답 3건 모두 중계 | 68 / 68.7 / 70 ms | 69.3 ms | 1280 / 1280 (IPv6) |
| `source-replication-to-target-db` | `PASS` | 중계 `tok`, 응답 3건 모두 중계 | 70 / 77 / 82 ms | 106.2 ms | 1280 / 1280 (IPv6) |

| 시험 | 목적지 쪽 두 경로 | 출발지 쪽 경로 |
| --- | --- | --- |
| 허용 | `bravo-source-db:5432` `connected` | `bravo-target-db:5432` `connected` |
| 포트 비교: 같은 상대 IPv6 주소의 5432 | `refused` | `refused` |
| 포트 비교: 같은 상대 IPv6 주소의 22 | `timeout` | `timeout` |
| 금지 | `bravo-target-db:5432` `no_tailnet_peer` | `bravo-source-db:5432` `no_tailnet_peer` |

세 경로 모두 대조군인 허용 경로와 금지 대상 DB가 살아 있었다. stderr는 비어 있었다.

## 03:08 목적지 쪽 실행

운영자 컴퓨터에서 목적지 클러스터 관리자 kubeconfig로 `--target-context`만 주고 돌렸다. 49초 걸렸다.

| 경로 | 판정 | 연결 방식 | Tailscale ping 왕복 (최소/평균/최대) | ICMP 왕복 | MTU / DF 최대 |
| --- | --- | --- | --- | --- | --- |
| `target-replication-to-source-db` | `PASS` | 중계 `tok`, 응답 3건 모두 중계 | 69 / 82.7 / 107 ms | 74.1 ms | 1280 / 1280 (IPv6) |
| `target-pooler-to-source-db` | `PASS` | 중계 `tok`, 응답 3건 모두 중계 | 69 / 69 / 69 ms | 69.5 ms | 1280 / 1280 (IPv6) |
| `source-replication-to-target-db` | `UNKNOWN` | 재지 못함 | - | - | - |

목적지 쪽 두 경로의 ACL 시험은 03:51과 같게 나왔다.

이때 출발지 경로를 못 잰 이유는 EKS API가 사설 전용(`endpointPublicAccess=false`)이라 운영자 컴퓨터에서 닿지 않았기 때문이다. 그래서 `context_not_provided`로 남겼고, 03:51에 위 방식으로 붙어 다시 쟀다.

이때 목적지 DB 프록시에서 본 `tag:bravo-source-replication`은 온라인이지만 핸드셰이크 기록이 없고 주고받은 바이트가 0이었다. 쉬고 있는 상대의 status `derp` 표시는 경로를 잰 값이 아니므로 연결 방식으로 쓰지 않았다.

## 복제 경로에서 관측한 것과 한계

복제는 02:59 KST에 재개됐다고 전달받았다. 구독 상태는 이 측정에서 읽지 않았다.

- 정방향 복제 경로는 03:08 실행, 두 번의 status 읽기(03:10:56, 03:12:02), 03:51 실행에서 모두 중계였다.
- 두 status 읽기 사이 66초 동안 보낸 쪽은 약 68 B/s, 받은 쪽은 약 94 B/s였다.
- 03:08 실행 뒤 netcheck에서 목적지 복제 프록시는 UDP를 쓸 수 있고, `MappingVariesByDestIP=true`이며 UPnP · PMP · PCP는 없다. 상대마다 바깥 포트가 달라지는 NAT로, 직접 연결을 어렵게 하는 조건 중 하나다. 출발지 쪽 NAT는 재지 않아 왜 중계인지는 정하지 않는다.
- 03:51 실행과 같은 시간대에 정방향 복제가 5초 시간 초과로 두 번 끊겼다. 이것은 T8에서 따로 다루고, 이 tailnet 판정과는 별개다.

**이 측정으로 복제 끊김의 원인을 정하지 않는다.** 이유는 다음과 같다.

- 끊긴 시각의 경로 값은 재지 않았다. 도구 값은 03:08과 03:51의 순간값이고, status 값은 03:10~03:12의 값이다.
- 트래픽이 거의 없는 상태의 값이다. 부하가 걸렸을 때의 왕복 시간과 처리량은 재지 않았다.
- 앱 · DB와 프록시 사이 구간은 재지 않았다.
- MTU 값의 범위가 좁다. 중계 구간은 TCP로 전달되므로, DF ping은 `tailscale0`이 받는 크기가 중계를 거쳐 돌아오는지만 본다. 직접 연결로 바뀌면 다시 재야 한다.

## 측정이 남긴 흔적

허용 시험은 상대 DB에, 금지 대상의 대조군 확인은 자기 쪽 DB에 데이터 없는 TCP 연결을 만든다. 두 실행 모두 양쪽 DB 로그에 시작 패킷 없는 연결 기록이 남을 수 있다. 로그를 직접 확인하지는 않았다.

`tailscale ping`은 쉬고 있던 경로를 깨워 직접 연결 시도를 일으킬 수 있다. 03:08 실행 뒤의 풀러 경로 status 값은 이 실행 직후의 값이다.
