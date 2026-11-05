# T30 tailnet 경로 점검

복제와 풀러가 쓰는 tailnet 경로가 지금 어떻게 이어져 있는지, ACL이 설계대로 열고 막는지 읽어서 JSON으로 알려 주는 도구다. 설정을 바꾸지 않는다.

**아직 실제 Tailscale에서 돌린 적이 없다.** 시험은 v1.104.4 소스로 확인한 출력 형식의 가짜 응답으로만 했다. [재지 못한 것](#재지-못한-것)을 먼저 본다.

## 실행

`python3`(3.9 이상)와 `kubectl`만 있으면 된다. 설치할 것은 없고, 저장소 루트에서 한 줄로 실행한다. 저장소가 없는 곳에서는 `infra/envs/target-workloads/`의 `tailnet_path_check.py`와 `network.py`를 같은 폴더에 두고 그 경로로 실행해도 된다.

두 클러스터에 모두 접속할 수 있으면 이렇게 한다.

```bash
python3 infra/envs/target-workloads/tailnet_path_check.py --source-context <출발지-context> --target-context <목적지-context>
```

목적지 VM 안에서는 이렇게 한다.

```bash
sudo env KUBECONFIG=/etc/rancher/rke2/rke2.yaml PATH="/var/lib/rancher/rke2/bin:$PATH" python3 infra/envs/target-workloads/tailnet_path_check.py --target-context default
```

**Tailscale 초대를 수락한 것만으로는 돌릴 수 없다.** 도구는 내 컴퓨터가 아니라 각 클러스터의 프록시 Pod 안에서 잰다. 내 컴퓨터는 역할 태그가 없어서 ACL 시험의 주체가 될 수 없다. 그래서 그 클러스터의 `kubectl` 접속이 필요하다.

| 필요한 권한 | 어디에 |
| --- | --- |
| `pods` get · list, `pods/exec` create · get | `tailscale-system` |
| `services` get | `source-dev`, `target-dev` 중 잴 쪽 |

한쪽 클러스터만 접속할 수 있으면 그쪽 인수만 준다. 재지 못한 경로는 `UNKNOWN`(`context_not_provided`)으로 남고 전체 결과도 `UNKNOWN`이다. 두 사람이 한쪽씩 돌렸다면 각자 결과에서 **자기 쪽 경로의 판정**을 읽는다. 다른 쪽 경로가 `UNKNOWN`인 것은 정상이다.

| 경로 | 필요한 클러스터 |
| --- | --- |
| `target-replication-to-source-db` | 목적지 |
| `target-pooler-to-source-db` | 목적지 |
| `source-replication-to-target-db` | 출발지 |

## 결과 읽는 법

| `overall` · 경로의 `verdict` | 뜻 | 종료 코드 |
| --- | --- | --- |
| `PASS` | 잰 값을 다 읽었고 판정 항목이 모두 맞다 | 0 |
| `FAIL` | 읽었는데 설계와 다르다. 하나라도 있으면 전체가 `FAIL`이다 | 1 |
| `UNKNOWN` | 못 읽은 값이 있다. **통과로 세지 않는다** | 2 |

경로마다 `reasons`에 이유가 붙는다. 그 아래에 재어 온 값이 있다.

```json
{
  "route": "target-replication-to-source-db",
  "side": "target",
  "from_tag": "tag:bravo-target-replication",
  "to_tag": "tag:bravo-source-db",
  "verdict": "PASS",
  "reasons": [],
  "path": {
    "kind": "derp",
    "derp_region": "tok",
    "peer_online": true,
    "pongs": {"direct": 0, "derp": 3, "peer_relay": 0},
    "disco_rtt_ms": {"samples": 3, "min": 31.0, "avg": 32.3, "max": 35.0},
    "verdict": "PASS"
  },
  "mtu": {"interface_bytes": 1280, "df_max_packet_bytes": 1280, "df_family": "ipv6", "icmp_rtt_ms": 33.1, "verdict": "PASS"},
  "acl": {
    "allow": {"target": "bravo-source-db:5432", "observed": "connected", "verdict": "PASS"},
    "deny": [
      {"probe": "wrong_port", "target": "bravo-source-db:22", "observed": "timeout", "control_observed": "refused", "verdict": "PASS"},
      {"probe": "forbidden_peer", "target": "bravo-target-db:5432", "observed": "no_tailnet_peer",
       "source_alive": true, "destination_alive": true, "verdict": "PASS"}
    ]
  }
}
```

위 값은 형식을 보여 주는 예시이고 측정값이 아니다.

| 값 | 뜻 |
| --- | --- |
| `path.kind` | `direct` 직접 연결, `derp` Tailscale 중계 서버 경유, `peer_relay` 다른 노드 경유. 읽지 못하면 `null` |
| `path.derp_region` | 중계 서버 지역. 중계가 아니면 `null` |
| `path.peer_online` | 제어 서버가 알려 준 상대의 접속 상태. 판정에 쓰지 않는다 |
| `path.disco_rtt_ms` | Tailscale 자체 ping의 왕복 시간. 마지막 경로의 표본만 센다. DB 지연이 아니다 |
| `mtu.interface_bytes` | `tailscale0`이 한 번에 보낸다고 약속한 패킷 크기 |
| `mtu.df_max_packet_bytes` | 쪼개지 말라고 표시(DF)한 ping이 실제로 돌아온 최대 패킷 크기 |
| `mtu.icmp_rtt_ms` | 작은 ping의 왕복 시간 |
| `path.verdict` | 경로를 읽었는지만 뜻한다. 직접 · 중계의 좋고 나쁨이 아니다 |
| `control_observed` | `wrong_port`의 대조군. 같은 IPv6 주소의 5432가 어떻게 답했는가 |
| `source_alive` | 이 Pod의 허용 경로가 같은 실행에서 연결됐는가 |
| `destination_alive` | 금지 대상 DB가 자기 프록시 뒤에서 응답하고, 그 노드가 보일 때는 터널도 살아 있는가 |

복제가 끊긴 원인을 찾을 때는 `path.kind`와 두 왕복 시간을 끊긴 시각 근처의 값과 나란히 본다. 이 도구는 실행한 순간의 값만 낸다.

## 무엇을 판정하고 무엇을 값만 내나

설계 문서와 저장소에서 근거를 찾은 것만 판정한다.

| 항목 | 판정 | 근거 |
| --- | --- | --- |
| 허용 경로의 TCP 5432 연결 | 연결돼야 한다 | `infra/envs/target-workloads/README.md`의 ACL 표, 설계 문서 데이터 경로 표. 경로 목록은 `network.py`에서 읽고, 시험이 README 표와 대조한다 |
| 자기 쪽 DB 5432(`forbidden_peer`) | 연결되면 안 된다 | 같은 표. 표에 없는 조합은 금지다 |
| 같은 상대의 22번(`wrong_port`) | ACL을 넘어가면 `FAIL` | 같은 표의 "TCP 5432만". 재는 방법은 아래에 있다 |
| 역할 태그 | 프록시와 상대의 태그가 맞아야 한다 | `network.py`가 렌더하는 태그 |
| DF 최대 크기 | `tailscale0` MTU보다 작으면 `FAIL` | 외부 숫자가 아니라 잰 값끼리의 비교다. 인터페이스가 약속한 크기를 경로가 버리면 TCP가 멈춘다 |
| 직접 · 중계 여부, 지역 | **값만** | 설계 문서는 중계를 위험으로 적었지 금지하지 않았다. 행사장 UDP 차단에 대비해 DERP 경유 리허설도 계획돼 있다 |
| 왕복 시간 | **값만** | 기준값이 없다. T8 복제 지연 기준은 이 값과 다른 것을 잰다 |
| 인터페이스 MTU 숫자 | **값만** | 1280은 Tailscale 기본값이고, 이 값을 요구하는 설계 조항은 없다 |

출력의 `not_judged`에도 같은 목록이 있다.

## 시험이 어떻게 재나

Pod 안에서 실행하는 명령은 아래 다섯 가지뿐이고, 도구가 다른 명령은 거부한다. Kubernetes에는 `get`과 `exec`만 쓴다.

| 명령 | 용도 |
| --- | --- |
| `tailscale status --json` | 태그, 상대 주소, 현재 경로 |
| `tailscale ping --c=3 --until-direct=false <상대>` | 경로와 왕복 시간 |
| `cat /sys/class/net/tailscale0/mtu` | 인터페이스 MTU |
| `ping -M do -s <크기> <상대>` | DF 최대 크기. 크기를 반씩 좁혀 찾는다 |
| `nc -vv -z -w 3 <상대> <포트>` | TCP 연결만 하고 데이터를 보내지 않는다. 허용 시험은 IPv4, 포트 비교는 IPv6 주소를 쓴다 |

`tailscale ping`은 설정을 바꾸지 않지만, 쉬고 있던 경로를 깨워 직접 연결 시도를 일으킬 수 있다.

흔히 틀리는 곳을 일부러 이렇게 처리했다.

- **`Relay`가 있다고 중계가 아니다.** 이 칸은 직접 연결 중에도 상대의 기본 중계 지역을 담는다. `tailscale status`처럼 `CurAddr`가 비었을 때만 중계로 본다.
- **연결이 안 됐다고 ACL이 막은 것은 아니다.** 터널 전체가 죽어도, 상대 DB가 꺼져 있어도 연결은 안 된다. 그래서 같은 실행에서 이 Pod의 허용 경로가 연결되고, 금지 대상 DB가 자기 프록시 안에서 응답할 때만 거부를 통과로 센다. 아니면 `UNKNOWN`이다.
- **거부는 `refused`가 아니라 `timeout`으로 보인다.** Tailscale은 ACL에 걸린 연결에 TCP 거절(RST)을 돌려주지 않는다. `refused`는 패킷이 ACL을 넘어갔다는 뜻이므로 통과로 세지 않는다.
- **금지 대상은 태그와 이름으로 모두 찾는다.** 다시 등록한 노드는 `-1`이 붙은 이름을 받아도 같은 권한을 가진다. 해당하는 노드가 여럿이면 모두 재고 가장 나쁜 결과를 쓴다.
- **목록에 없는 상대에게는 TCP를 시도하지 않는다.** tailnet 터널이 없어 Pod의 기본 경로로 나가므로 다른 것을 재게 된다. 목록에 없다는 사실(`no_tailnet_peer`)을 결과로 쓴다. 목록에 보이는 금지 대상은 TCP와 함께 Tailscale ping도 보내, 터널이 살아 있을 때만 `timeout`을 거부로 센다.
- **포트 제한은 IPv4 주소로 잴 수 없어 IPv6 주소에서 비교한다.** DB 쪽 프록시는 IPv4 tailnet 주소로 온 패킷을 Service로 넘기고 Service는 5432만 연다. ACL이 22번을 열어 줘도 받는 쪽이 없어 차이가 나지 않는다. IPv6 주소는 넘기지 않으므로 두 포트 모두 듣는 곳이 없다. ACL이 허용한 5432는 프록시 커널이 `refused`로 답하고, 막힌 22번은 답이 없을 것이다. 그래서 5432가 답하고 22번이 조용하면 `PASS`, 22번이 `refused`나 연결이면 `FAIL`(`wrong_port_open`), 5432도 조용하면 비교가 안 되므로 `UNKNOWN`이다. PeerAPI 포트는 ACL과 상관없이 통과하므로(`net/tstun/wrap.go`) 이 비교에 쓰지 않는다.
- **DF ping은 IPv4 다음에 IPv6를 시도한다.** DB 쪽 프록시는 자기 IPv4 tailnet 주소로 온 모든 패킷을 Service로 넘기고, Service 주소가 ping에 답하는지는 kube-proxy 방식에 따라 다르다. IPv6 주소는 넘기지 않으므로 프록시가 직접 답할 것으로 본다. 사용한 쪽이 `df_family`에 나온다.
- **가장 큰 크기의 ping은 크기를 찾은 뒤 한 번 더 묻는다.** 경로가 자리 잡는 동안의 손실을 MTU 문제로 읽지 않기 위해서다.

## `reasons` 코드

| 코드 | 판정 | 뜻 | 할 일 |
| --- | --- | --- | --- |
| `context_not_provided` | UNKNOWN | 그쪽 context를 주지 않았다 | 접속할 수 있는 사람이 돌린다 |
| `kubectl_unavailable` | UNKNOWN | `kubectl`이 없거나 30초 안에 끝나지 않았다 | 설치와 클러스터 접속을 확인한다 |
| `kubernetes_forbidden` | UNKNOWN | 프록시 Pod를 읽거나 실행할 권한이 없다 | [실행](#실행)의 권한을 받는다. 권한을 넓혀 달라고 하기 전에 대상 context가 맞는지 본다 |
| `kubernetes_read_failed` | UNKNOWN | Kubernetes 읽기 실패 | context와 API 서버 상태를 본다 |
| `proxy_pod_not_unique` | UNKNOWN | 실행 중인 프록시 Pod가 정확히 하나가 아니다 | ProxyGroup 상태와 잘못된 context인지 본다 |
| `status_read_failed`, `status_unparseable` | UNKNOWN | `tailscale status`를 못 읽었다 | Pod 로그를 본다. 버전이 바뀌었다면 출력 형식을 다시 확인한다 |
| `tailscale_not_running` | UNKNOWN | 프록시가 로그인되지 않았다 | operator와 OAuth 상태를 본다 |
| `peer_address_unread` | UNKNOWN | 상대의 IPv4 주소가 없다 | tailnet 설정을 본다 |
| `no_pong` | UNKNOWN | Tailscale ping이 돌아오지 않았다 | 상대가 켜져 있는지, 경로가 끊겼는지 본다 |
| `path_status_unread` | UNKNOWN | ping은 돌아왔는데 status가 현재 경로를 말해 주지 않았다 | 다시 돌린다. 반복되면 `tailscale status`를 Pod에서 직접 본다 |
| `path_status_disagrees_with_ping` | UNKNOWN | ping과 status가 다른 경로를 말한다. 경로가 바뀌는 중일 수 있다 | 잠시 뒤 다시 돌린다 |
| `interface_mtu_unread` | UNKNOWN | `tailscale0` MTU를 못 읽었다 | 프록시가 커널 모드인지 본다 |
| `df_probe_unread` | UNKNOWN | DF ping 결과가 ping 요약이 아니거나, 재는 도중 작은 크기도 답이 끊겼다. `-M`을 못 쓰는 ping이거나 권한이 없을 수 있다 | Pod 안의 `ping -V`를 보고 다시 돌린다 |
| `df_probe_no_reply` | UNKNOWN | 가장 작은 ping에도 답이 없다. MTU가 작다는 뜻이 아니다 | 상대의 IPv6와 ICMP 경로를 본다 |
| `allow_probe_unread`, `deny_probe_unread` | UNKNOWN | `nc`의 결과가 아니거나, 금지 대상에 IPv4 주소가 없다 | 같은 명령을 Pod에서 직접 실행해 출력을 본다 |
| `deny_control_missing` | UNKNOWN | 대조군이 없다. 허용 경로가 안 되거나, 금지 대상 DB가 응답하지 않거나, 보이는 금지 대상의 터널이 죽었다. `services` 읽기 권한이 없어도 여기에 나온다 | 해당 DB와 허용 경로, 권한부터 고친다 |
| `deny_refused_acl_unproven` | UNKNOWN | 금지 대상이 `refused`로 답했다. ACL이 막지 않았을 수 있다 | tailnet 정책을 본다 |
| `self_tag_mismatch`, `peer_tag_mismatch` | FAIL | 태그가 설계와 다르다. ACL 시험은 하지 않는다 | 태그와 ProxyGroup 설정을 본다 |
| `peer_not_visible` | FAIL | 허용된 상대가 이 노드의 목록에 없다 | ACL, 상대의 등록 상태, 이름 충돌(`-1` 접미사)을 본다 |
| `allow_not_connected` | FAIL | 허용 경로가 연결되지 않는다 | ACL, 프록시, DB 순으로 본다 |
| `deny_connected` | FAIL | 금지 대상 DB에 연결됐다 | **tailnet의 넓은 허용 규칙을 먼저 본다** |
| `wrong_port_open` | FAIL | 22번이 `refused`이거나 연결됐다. 패킷이 ACL을 넘어갔다는 뜻이다 | 이 역할에 포트 전체를 여는 규칙이 있는지 본다 |
| `wrong_port_probe_unread` | UNKNOWN | 포트 비교의 `nc` 결과가 아니거나, 상대에게 IPv6 주소가 없다 | 상대의 IPv6 주소와 같은 명령의 출력을 본다 |
| `wrong_port_control_missing` | UNKNOWN | 같은 주소의 5432도 답이 없어 비교할 수 없다 | Pod의 IPv6와 ACL이 IPv6 주소에도 적용되는지 본다 |
| `df_below_interface_mtu` | FAIL | 인터페이스 MTU보다 작은 크기까지만 지나간다 | 경로의 MTU를 조사한다 |
| `unexpected_<종류>` | UNKNOWN | 예상하지 못한 오류. 종류 이름만 남긴다 | 도구를 고친다 |

## 결과를 남길 때

출력에는 tailnet 주소, 상대의 실제 주소, 이메일, tailnet 이름, 계정 ID, kubectl context를 넣지 않는다. 명령의 오류 문구도 복사하지 않는다. 시험이 가짜 값을 심어 확인한다.

`from_tag`, `to_tag`, 경로 이름은 저장소에 이미 공개된 이름이다. `derp_region`은 공용 DERP라면 `tok` 같은 공용 지역 코드이고, 자체 DERP를 쓰면 그 이름이 그대로 나온다. 그래도 공개 저장소에 올리기 전에는 [공개 증빙 절차](../public-evidence-policy.md)대로 원본을 비공개로 보존하고 `python3 scripts/public_evidence.py check`를 돌린다. `source_sha256`은 실행한 도구 파일의 해시다.

## 재지 못한 것

- **실제 Tailscale, 실제 클러스터, 실제 VM에서 한 번도 실행하지 않았다.** 실제 첫 실행 전까지 `PASS`를 포함한 모든 판정은 가짜 응답에 대한 것이다.
- **고정 이미지 안의 명령을 확인하지 않았다.** `network.py`가 고정한 `tailscale/tailscale` 이미지에 iputils `ping`과 BusyBox `nc`가 있고, 소켓이 `/tmp/tailscaled.sock`이라는 것은 v1.104.4 소스로 읽었다. 이미지를 받아 돌려 보지는 않았다. 없으면 `UNKNOWN`으로 나온다.
- **`nc`와 `ping` 문구.** `nc`는 Alpine 3.24의 BusyBox 1.37에서 직접 받은 문구이고, 이미지의 Alpine 3.22와 판이 다를 수 있다. `ping`은 iputils 20240905 소스에서 읽은 문구다. 다르면 `UNKNOWN`으로 나오고 통과로 세지 않는다.
- **ACL 거부가 `timeout`으로 보이는 것과 IPv6 DF ping에 프록시가 답하는 것**은 소스를 읽은 결론이다. 받는 쪽은 거절 통지(TSMP)도 보내는데, 보낸 쪽 커널이 어떻게 반응하는지는 재지 않았다. 실제 Pod에 IPv6가 꺼져 있으면 `df_probe_no_reply`나 `df_probe_unread`로 남는다. 그 환경에서는 MTU와 포트 비교 때문에 경로가 `PASS`가 될 수 없고, MTU 값은 `interface_bytes`까지만 나온다.
- **`PASS`는 프록시에서 상대 노드까지만 뜻한다.** 앱이나 DB에서 자기 쪽 프록시까지의 구간과 프록시에서 DB까지의 구간, 그 구간의 MTU는 재지 않는다. 이 범위는 출력의 `not_measured`에도 적혀 있다.
- **22번 말고 다른 포트 하나만 여는 규칙, IPv4 주소를 직접 지정해 연 규칙.** IPv6 주소의 22번만 비교하므로 보이지 않는다. 태그에 포트 전체를 여는 규칙은 잡는다. 제어 서버가 태그 규칙에 IPv6 주소도 넣는다는 전제이고, 아니면 `wrong_port_control_missing`으로 남는다.
- **포트 비교 자체.** IPv6 주소에 5432는 `refused`, 막힌 포트는 무응답이라는 것은 소스를 읽은 결론이다. 실제 실행에서 이렇게 나오지 않으면 비교를 더 고치지 않고, 이 판정을 빼서 [tailnet 정책의 `tests`](https://tailscale.com/kb/1337/policy-syntax#tests)로 확인하는 쪽으로 넘긴다.
- **`pods/exec` 권한.** `kubectl` 판에 따라 `create`나 `get`을 쓴다. 정확히 어느 쪽이 필요한지 실제 클러스터에서 확인하지 않아 둘 다 적었다. 모자라면 `kubernetes_forbidden`으로 나온다.
- **이 세 경로 밖.** `bravo-source-pooler`, 컷오버 제어 풀러, Collector, 부하 생성기 경로는 재지 않는다.

## 시험

```bash
python3 -m unittest discover -s scripts/tests -p 'test_tailnet_path_*.py'
```

`test_tailnet_path_check.py`는 직접 · 중계 · 읽기 실패 · 명령 실패 · 허용과 거부가 뒤바뀐 경우를 가짜 응답으로 잰다. `test_tailnet_path_mutations.py`는 임시 복사본에서 규칙을 하나씩 빼고 시험이 실패하는지 확인한다.
