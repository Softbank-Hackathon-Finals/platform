# 목적지 비밀정보 전달 개발 실측

2026-11-04 KST에 별도 개발 VM에서 고정된 출발지 시크릿 버전을 OpenBao로
옮기고 ESO를 거쳐 소비자 파일에 전달했다. [공개 readback](readback.json)은
비공개 원본 12개의 해시와 관측 시각을 결속한 선택 공개본이다. 실제 운영
식별자·자격 값은 포함하지 않는다. 검토 권한이 있는 사람은 원본과 대조해야
하며, 공개본만으로 실제 클러스터 신원을 독립 확인할 수는 없다.

| 경계 | 실제 관측 |
| --- | --- |
| 1회 이관 | 고정 VersionId + AWSCURRENT, KV CAS 0, 출발지 변경 0, 값 파일 0 |
| ESO → 소비자 | 세 ExternalSecret 동기화, 실제 원본 앱 이미지의 제한된 Python 소비자에서 19/19 PASS |
| 허용·거부 | 지정 SA의 정확 경로 읽기 및 전달값 일치; 무인증/다른 SA/다른 경로/덮어쓰기/토큰 생성 거부 |
| TLS | 기본 엄격 검증, 신뢰되지 않는 CA와 잘못된 호스트 거부 |
| 재시작 | 기존 PV·초기화 복구, 세 KV의 값과 메타데이터 그대로, Ready |
| 갱신 | 아직 앱에 쓰이지 않는 목적지 admin 토큰만 CAS 1→2; Secret과 기존 소비자의 파일까지 일치 |
| 정리 | 시험 Pod와 NetworkPolicy만 UID 조건으로 삭제 |
| 플랫폼 DB 네트워크 | 전용 Executor용 정책 2개 추가, 기존 정책 사양 보존; 실제 Executor 트래픽은 미검증 |

초기 TLS 인증서는 Go 기반 서버/ESO 연결에는 동작했으나 실제 Python 소비자의
엄격 검증에서 `Missing Authority Key Identifier`로 거부됐다. 같은 CA·서버
키를 유지한 채 SKI/AKI와 leaf KeyUsage를 보완하고, 검증을 완화하지 않은 상태로
실제 소비자 시험을 다시 통과시켰다. 초기 실패는 성공으로 대체해 기록하지 않는다.

공식 OpenBao 2.7.2 이미지의 수정 가능한 go-jose HIGH 문제와 첫 빌드의 Go
도구 체인 문제는 고정 소스·go-jose 4.1.6·Go 1.27.3으로 수정했다. 이미지에
`USER 100:1000`도 명시했다. 최종 OCI의 OS/Go 바이너리 취약점 및 시크릿 검사,
Dockerfile HIGH/CRITICAL 구성 검사는 예외 추가 없이 통과했다. 이미지 포장
버전은 `bravo.3-nonroot`, 바이너리 버전은 `2.7.2+bravo.3`이다.

`publication_code_sha256`은 공개 당시 코드이고, 과거 실행 소스가 그 바이트와
모두 같았다는 뜻이 아니다. 초기 배포, 엄격 TLS 보완, 이미지 교체, 재시작 및
소비자 시험은 각각의 원본 영수증·시각으로 구별한다. OpenBao는 개발용 단일
노드/수동 unseal이며, 원격 백업·HA·자동 복구를 입증하지 않는다.

이 시험은 앱 전체 lifespan을 실행하지 않았다. T15의 실제 승인된 번역과 T31
이미지, 앱의 300초 자격 캐시 동작, 전체 T9 통제 및 실제 cutover/SHIFT는 별도다.
기존 `target-app-db`, DB LOGIN/구독, 업무 데이터와 원본 자격은 이 작업에서
변경하지 않았다. 재현 구성은 [target-secrets 안내](../../../infra/envs/target-secrets/README.md)를 따른다.

[네트워크 추가 readback](network-readback.json)은 전용 플랫폼 DB·PgBouncer
경로 4개와 source PgBouncer의 target DB 전용 경로 생성 근거 7개를 별도로
결속한다. 실제 Service UID·동적 포트, proxy Pod UID와 tailscaled 소유 UDP
소켓을 확인하고 수신·송신 정책을 적용했다. 기존 복제 그룹과 Service,
기존 정책 사양은 보존했다. 호출자 업무 경로와 operator 상태 확인을 분리한다.

첫 플랫폼 접속 시험은 CPU 예약량 부족으로 Pod가 배치되지 않아 네트워크
호출 전에 중단됐다. 이 readback의 Ready·정책 적용은 실제 연결 성공이나
잘못된 호출자의 차단을 입증하지 않는다. T12의 실제 계정·TLS 및 호출자
허용/거부 시험은 별도 통합 근거가 필요하다. source pooler의 backend,
hostAlias와 DB 역할은 이 네트워크 작업에서 변경하지 않았다. proxy 재생성 시
UDP 소켓·Service 포트가 바뀔 수 있어 현재 값으로 결속한 개발 정책을 다시
조회·검토해야 한다.

[source EKS 후속 수정 실측](source-cni-repair-readback.json)은 위 적용 기록 뒤의
실패와 수정 결과를 원본 11개 해시로 결속한다. 첫 실제 source pooler 시험은
정상 TLS 접속 전후에 잘못된 표식의 호출자도 TCP 접속에 성공해 실패했다.
원인은 정책 객체만 생성돼 있고 CNI의 실제 정책 집행 기능은 비활성인 상태였다.
고정된 버전을 유지하며 기능을 켜고, 정확한 프록시와 호출자를 연결하는
PolicyEndpoint를 확인했다.

첫 업데이트 요청에 기존 IAM 역할을 명시하지 않아 새 CNI Pod에 역할 주입이
빠졌으며, 업데이트가 끝난 뒤 add-on 역할 필드도 비어 있었다. 기존 역할을
명시해 복원하고 add-on·ServiceAccount·새 Pod가 동일한 역할을 쓰며
ACTIVE/Ready인 것을 확인했다. 노드 권한 확대는 하지 않았고 기존 업무
Pod UID도 유지됐다. 실패한 CNI Pod만 UID 조건으로 교체했다.

이후 실제 source PgBouncer의 엄격 TLS 정상 접속 → 잘못된 표식의 호출자
거부 → 정상 재접속이 통과했고, 다른 네임스페이스의 호출자에도 같은 순서를
반복해 통과했다. 거부 시험은 자격 없이 생성된 Service IP로 새 TCP 연결을
시도했으며, 호출자 쪽 송신 정책은 없어 그 정책의 차단으로 가려진 시험이
아니다. 임시 Job·Pod 부재, 업무 DB 쓰기 0건도 확인했다. 실제 업무 backend와
LOGIN 상태는 유지했다. 표준 CNI 시작 모드는 정책이 적용되기 전 허용 구간이
있으므로 Pod 교체 뒤 이 결과를 그대로 재사용하지 않는다.
