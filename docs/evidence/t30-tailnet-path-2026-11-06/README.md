# T30 tailnet 경로 첫 실제 측정

2026-11-06 03:07~03:12 KST. **목적지 쪽 두 경로는 `PASS`이고 둘 다 DERP(Tailscale 중계 서버) `tok`을 지났다. 출발지 쪽 `source-replication-to-target-db`는 재지 못해 `UNKNOWN`이고, 전체 결과도 `UNKNOWN`(종료 코드 2)이다.** 통과로 세지 않는다.

해석과 실행 방법은 [운영 문서](../../operations/t30-tailnet-path-check.md)에 있다.

| 파일 | 내용 |
| --- | --- |
| `result.json` | 도구 출력 그대로. 가릴 값이 없어 비공개 원본과 바이트가 같다 |
| `supplement.json` | 도구 실행 뒤에 따로 읽은 `tailscale status`와 `tailscale netcheck`. 정한 필드만 남겼고 원본 JSON은 저장하지 않아 대조할 원본이 없다. 판정이 아니다 |
| `source-manifest.json` | 측정한 커밋 `b2a5b50`의 파일 해시. `result.json`의 `source_sha256`과 같은 도구다 |

운영자 컴퓨터에서 목적지 클러스터 관리자 kubeconfig로 `--target-context`만 주고 한 번 돌렸다. 도구는 프록시 Pod 안에서 읽기 명령과 TCP 연결만 했고, 49초 걸렸다. 설정, ACL, 프록시는 바꾸지 않았다.

## 경로별 결과

| 경로 | 판정 | 연결 방식 | Tailscale ping 왕복 (최소/평균/최대) | ICMP 왕복 | MTU / DF 최대 |
| --- | --- | --- | --- | --- | --- |
| `target-replication-to-source-db` | `PASS` | 중계 `tok`, 응답 3건 모두 중계 | 69 / 82.7 / 107 ms | 74.1 ms | 1280 / 1280 (IPv6) |
| `target-pooler-to-source-db` | `PASS` | 중계 `tok`, 응답 3건 모두 중계 | 69 / 69 / 69 ms | 69.5 ms | 1280 / 1280 (IPv6) |
| `source-replication-to-target-db` | `UNKNOWN` | 재지 못함 | - | - | - |

목적지 쪽 두 경로의 ACL 시험은 같게 나왔다.

| 시험 | 결과 |
| --- | --- |
| 허용: `bravo-source-db:5432` | `connected` |
| 포트 비교: 같은 상대 IPv6 주소의 5432 | `refused` (패킷이 ACL을 넘어갔다) |
| 포트 비교: 같은 상대 IPv6 주소의 22 | `timeout` (`refused`가 아니다) |
| 금지: `bravo-target-db:5432` | `no_tailnet_peer` (이 프록시 목록에 없다). 대조군인 허용 경로와 대상 DB는 살아 있었다 |

## 출발지 경로를 왜 못 쟀나

도구는 출발지 프록시 Pod 안에서 재야 하고, 그러려면 출발지 EKS API에 닿아야 한다. EKS API가 사설 전용(`endpointPublicAccess=false`)이어서 운영자 컴퓨터에서 닿지 않았고, 출발지 VPC 안에서 돌리지는 않았다. 그래서 `context_not_provided`로 남겼다.

목적지 DB 프록시에서 본 `tag:bravo-source-replication`은 온라인이지만 핸드셰이크 기록이 없고 주고받은 바이트가 0이었다. 지금 살아 있는 세션이 없다는 뜻이다. status의 `derp` 표시는 쉬는 상태의 표시이므로, 이 경로의 연결 방식으로 읽지 않는다.

## 복제 경로에서 관측한 것과 한계

복제는 02:59 KST에 재개됐다고 전달받았다. 구독 상태는 이 측정에서 읽지 않았다.

- 복제 경로는 도구 실행(03:08)과 두 번의 status 읽기(03:10:56, 03:12:02)에서 모두 중계였다.
- 두 status 읽기 사이 66초 동안 보낸 쪽은 약 68 B/s, 받은 쪽은 약 94 B/s였다.
- netcheck에서 복제 프록시는 UDP를 쓸 수 있고, `MappingVariesByDestIP=true`이며 UPnP · PMP · PCP는 없다. 상대마다 바깥 포트가 달라지는 NAT로, 직접 연결을 어렵게 하는 조건 중 하나다. 출발지 쪽은 재지 못해 왜 중계인지는 정하지 않는다.

**이 측정으로 복제 끊김의 원인을 정하지 않는다.** 이유는 다음과 같다.

- 복제가 끊긴 시각의 값이 아니다. 03:07~03:12 KST 약 5분의 값이다.
- 도구 측정은 한 번뿐이다. 시간차 반복은 하지 않았다.
- 트래픽이 거의 없는 상태의 값이다. 부하가 걸렸을 때의 왕복 시간과 처리량은 재지 않았다.
- 앱 · DB와 프록시 사이 구간은 재지 않았다.
- MTU 값의 범위가 좁다. 중계 구간은 TCP로 전달되므로, DF ping은 `tailscale0`이 받는 크기가 중계를 거쳐 돌아오는지만 본다. 직접 연결로 바뀌면 다시 재야 한다.

## 측정이 남긴 흔적

허용 시험은 출발지 DB에, 금지 대상의 대조군 확인은 목적지 DB에 데이터 없는 TCP 연결을 만들었다. 두 DB 로그에 시작 패킷 없는 연결 기록이 남을 수 있다. 로그를 직접 확인하지는 않았다.

`tailscale ping`은 쉬고 있던 경로를 깨워 직접 연결 시도를 일으킬 수 있다. 풀러 경로의 status 값은 이 실행 직후의 값이다.
