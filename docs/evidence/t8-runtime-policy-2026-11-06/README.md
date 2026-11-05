# T8 실제 설정 정합과 오류 감시 — 2026-11-06

11월 6일 05:53 KST에 ARM 이관 담당의 같은 실행에서 정방향 `disable_on_error=false`, 역방향 `true`, 양쪽 송신·수신 timeout 30초를 적용했다. 렌더 CLI, Kubernetes 객체, PostgreSQL 값을 대조했고 독립 재조회도 통과했다. 양쪽 DB 지표가 실제 Prometheus에 도착하며, 출발지 지표 누락 경보가 `firing`에서 `inactive`로 바뀐 것을 확인했다. 숫자·시각·원본 해시는 [result.json](result.json)에 있다.

## 변경한 이유와 실행 경계

04:16:08 KST의 `true` 변경은 기존 T17 Edge 복구 실행이 과거 T36 인계의 `true 유지` 조건을 복원하면서 발생했다. 태현님의 방향별 렌더 수정이 원인이 아니다. 변경 의도와 전후 객체의 대조는 [#382 조사 댓글](https://github.com/Softbank-Hackathon-Finals/platform/pull/382#issuecomment-6667870473)에 남겼다.

[#382](https://github.com/Softbank-Hackathon-Finals/platform/pull/382)의 렌더 head `6a343409e362843c208008394dbaf8b7e2e61a7d`는 이미 정방향 `false`·역방향 `true`·네 timeout 30초였다. 사용자가 추가 답변을 기다리지 말고 직접 해결·병합하라고 지시한 뒤, ARM 담당은 DB·VM 복구와 최종 정책 변경을 단독 실행했다. 이 PR의 작업자는 Collector·관측 네트워크·Prometheus 규칙과 독립 검증을 맡았다. 같은 DB를 두 실행자가 수정하지 않았다.

## 렌더·객체·DB 대조

| 대상 | 렌더 CLI | 실제 Kubernetes | 실제 DB |
|---|---|---|---|
| source 송신·수신 timeout | 각각 `30s`, exit 0 | 각각 `30s` | 각각 `30000ms`, pending restart 없음 |
| target 송신·수신 timeout | 각각 `30s`, exit 0 | 각각 `30s` | 각각 `30000ms`, pending restart 없음 |
| target 정방향 구독 | `disable_on_error=false`, enabled | `false`, applied, observedGeneration 일치 | `false`, ON, apply worker 1, 오류 0 |
| source 역방향 구독 | `disable_on_error=true`, disabled | `true`, applied, observedGeneration 일치 | `true`, OFF, worker 0 |

정책 변경 전후 DB Pod UID·재시작 횟수·업무 행 해시는 유지됐다. 새 ENABLE·marker 삽입·업무 쓰기는 0이다. 양 앱의 쓰기 차단과 target 앱 역할 NOLOGIN은 유지했다. ARM 복원으로 target의 PostgreSQL WAL 도메인은 바뀌었으며 기존 원본과 같다고 판단하지 않는다. VM·데이터 복원 원장은 ARM 담당이 보존한다.

## 실제 감시 경로 복구

- **source:** 기존 Collector는 앱·자체 지표만 수집했다. 앱 mTLS 인자를 보존하고 DB 파이프라인을 추가했다. 재생성 전에 큐 0과 자체 지표 시각 전진을 두 번 확인했다. `source-dev` Pod `get/list/watch`만 허용하는 Role을 추가했고, CNPG Pod의 9187과 관측한 Kubernetes API Service·endpoint의 개별 주소만 허용했다. Secret·exec 권한은 추가하지 않았다.
- **전용 tailnet 경로:** source·target Collector 태그에서 metrics 태그의 TCP 9090만 추가했다. 38개 정책 검사를 포함한 저장 후 readback이 의도한 정책과 일치했다. source EKS에서는 Service DNAT 전 정책 처리가 관측되어 검증한 child Service의 단일 주소 `/32:9090` 예외가 필요했다. 새 CLI `--service-ip-egress`는 이 경우에만 사용한다. Service CIDR 전체를 열지 않는다. 기존 접근 실패로 source proxy의 forwarding 설정이 비어 있었으므로 해당 전용 proxy Pod만 UID 조건으로 갱신했다. 실제 UDP 소켓과 변경된 Pod 신원으로 ingress를 다시 결속했다.
- **target mTLS:** 기존 발급자 개인키는 원 발급 담당의 PC에만 있었고 이 실행에서 사용할 수 없었다. target에서 새 개인키·CSR을 만들고, DNS `collector-onprem`만 허용하는 제한 CA로 clientAuth 전용 7일 인증서를 발급했다. target 개인키는 guest 밖으로 복사하지 않았고 CA 개인키도 공유하지 않았다. platform에는 해당 공개 CA만 추가했다. 기존 source 신뢰와 서버 인증서·개인키는 유지했다. 실제 양쪽 mTLS 요청이 성공했다.

source의 허용 경로는 성공했고, 임시 시험 예외를 제거하자 `app=bravo-collector`가 아닌 같은 시험 Pod는 시간 초과로 차단됐다. target에서는 잘못된 label과 namespace를 각각 차단했다. source 시험 Deployment·Pod·정책 2개, target 소유 시험 Pod는 삭제됐고 이 작업이 만든 SSM 세션의 활성 잔존은 0이다.

재배포 시 source의 기존 `collector-app-mtls.yaml` 인자를 지우지 않고 `collector-db.yaml`을 함께 사용한다. DB 파이프라인은 `BRAVO_DB_NAMESPACE=source-dev`와 해당 namespace의 discovery Role을 함께 요구한다. target은 `target-dev`를 쓴다. 렌더에 고정하지 않은 child Service IP·proxy UDP 포트·API endpoint는 실제 UID와 주소를 다시 읽어 결속한다. 과거 관측값을 새 Pod에 그대로 적용하지 않는다.

## 검증한 감시 동작

05:54–05:56 KST 독립 조회에서 양쪽 `bravo_signal=cnpg-scrape`의 `up=1`과 표본 시각 전진을 확인했다. source 정방향 슬롯은 active 1·관측 WAL 차이 56바이트, target 구독은 enabled 1·worker 1·적용 오류 counter 0이었다. 양쪽 exporter 큐는 0이었다. 조회 시각을 표본 생성 시각으로 대신하지 않았다.

Prometheus에 정방향 OFF·오류 증가·반복 오류·슬롯 비활성·구독 지표 누락·슬롯 지표 누락의 6개 경보를 추가했다. 배포 파일과 저장소 파일의 SHA-256이 같고, 원격 `promtool`은 총 17개 규칙을 통과했다. ConfigMap 수정 후 SIGHUP으로 반영해 Prometheus Pod와 restart 0을 유지했으며, emptyDir 시계열 저장소를 재시작으로 잃지 않았다.

출발지 지표가 없던 05:47 KST에는 `BravoForwardSlotUnobservable`이 실제 `firing`이었다. 수집 복구 후 6개 정방향 경보는 모두 `inactive`, 17개 규칙의 health는 모두 `ok`다. **운영 DB에 오류를 새로 주입하지 않았다.** 오류 증가·반복·counter reset·앱 출처 사칭·누락·복구는 배포 Prometheus 이미지의 `promtool` 합성 시계열 시험으로 확인했다. 외부 Slack·메일 알림 발송은 이번 검증 범위에 없다.

관련 검증은 observability 190개, 복제 48개, Collector route 22개가 통과했다. Ruff와 변경 파일 공백 검사도 통과했다. 세부 실행 로그는 비공개로 보존하며 공개 자료에는 결과와 원본 해시만 싣는다.

## 되돌리기와 만료

1. **DB 정책:** 같은 ARM/후속 단일 운영 실행에서 현재 writer·구독 상태를 확인한 뒤 [운영 절차](../../operations/t8-replication-health.md)의 정방향 `true` 복구와 필요시 양단 timeout 이전값 복구를 수행한다. 객체·DB·observedGeneration을 다시 대조한다. 역방향 ENABLE이나 writer 전환을 이 복구에 끼워 넣지 않는다.
2. **감시 규칙:** 비공개 원장의 `platform-rules-before.private.json`에서 원래 `alerts.yml`만 복원한다. 현재 ConfigMap UID/resourceVersion을 확인하고, 원격 promtool 검사 후 SIGHUP한다. Deployment나 Prometheus Pod를 재시작하지 않는다. 복구 전후 규칙 hash를 남긴다.
3. **source Collector:** `source-db-intent.private.json`에 보존한 Deployment 인자·env·ServiceAccount 토큰 설정으로 복원하되, 먼저 큐 0을 확인한다. 이번에 만든 immutable env ConfigMap·DB discovery Role/Binding·DB egress 정책은 소유와 참조 부재를 확인한 뒤 제거한다. 이전 앱 mTLS 설정을 유지한다. 관측 경로를 끄면 지표 누락 경보가 다시 뜨는 것이 정상이다.
4. **네트워크:** Service/Pod 교체 때 새 child IP와 proxy의 실제 UDP 소켓을 다시 결속한다. 되돌릴 때는 `source-network-intent.private.json`의 이전 정책을 UID/resourceVersion으로 대조한다. tailnet에서는 이번 Collector 두 태그 → metrics TCP 9090 grant와 그에 대응하는 시험만 되돌린다. 다른 grant·사용자·tagOwner는 보존한다.
5. **임시 발급자 회수:** target leaf는 **11월 13일 05:41:27 KST**, 제한 CA는 **11월 20일 05:36:50 KST**에 만료된다. 만료 전에 기존 발급자의 target 인증서로 교체하거나 같은 최소 범위로 갱신해야 한다. target 개인키를 밖으로 옮기지 않는다. 새 leaf의 실제 mTLS 수신을 확인한 뒤 platform client CA bundle에서 이번 제한 CA 한 장만 제거한다. 기존 source CA와 서버 키는 보존한다. 인증서 지문·만료는 result.json에 있다.

이 기록은 T8 정책 불일치와 감시 누락의 해결이다. T8 전체 지연·데이터 이관 완료, ARM 환경의 전체 성능 검증, S1→S4 컷오버 완료를 뜻하지 않는다.
