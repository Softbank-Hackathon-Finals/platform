# 실제 워크로드 관측과 Sova 지도

상태: 실제 온프렘 GET 관측을 기존 Sova 화면에 연결한 개발 인수 단계다.
운영 T10 호스트 배포, 실제 양쪽 환경 인수와 보안 대응 실행 완료는 아니다.
실제 관측·시험 범위와 생산자 출처는 [개발·인수 기록](live-workload-development-2026-11-08.md)에 남긴다.

## 표시 범위

- 등록한 클러스터 UID·환경·namespace·Service 목록만 사용한다. 브라우저에서 대상이나 파일 경로를 고를 수 없다.
- Service selector로 연결된 Pod, Pod 안의 컨테이너, 등록된 Service를 가리키는 일부 명시적 환경 설정을 관계로 표시한다.
  이 회색 선은 실제 설정 관계이며, 통신·복제 성공의 근거가 아니다. Secret 값·로그·임의 명령은 수집하지 않는다.
- Pod의 Ready 조건과 컨테이너 준비 상태를 분리한다. 컨테이너가 Ready라도 Pod readiness gate를 통과했다고 추정하지 않는다.
- 명시적으로 등록한 Service의 API 프록시 `/healthz` 응답이 `ok=true`인 경우만 HTTP 근거를 만든다.
  같은 수집 중 Service UID·설정과 클러스터 UID를 다시 확인한다. 이 근거는 Service에만 적용하고 Pod·DB·업무 요청의 성공으로 복사하지 않는다.
- 파란 선은 위 관리 HTTP 탐침이다. 사용자 트래픽의 양이나 패킷 이동을 실측한 애니메이션이 아니다.
- NetworkPolicy 수는 설정 개수다. 적용 효과·차단·보안 동등성을 뜻하지 않는다.
- 수집기는 보안 PASS·공격·보류·격리·대체·복구를 판단하지 않는다. 이 관측에서 해당 주장을 넣으면 거부한다.

기존 Sova 사이드바·카드·SVG 렌더러·확대/선택을 재사용한다. 관측 전용 모드는 다른 조작 메뉴를 비활성화하고,
운영 계획·승인·게이트·변경 API를 제공하지 않는다. 기존 일반 콘솔의 동작을 교체하지 않는다.

## 수집과 출처

`kubernetes_workload.py`는 config view 및 허용한 GET만 실행한다. exec/apply/delete/logs/Secret,
임의 프록시 경로와 모든 namespace 조회는 클라이언트 계층에서 거부한다. TLS 검증을 끄거나 HTTP API를 사용하지 않는다.
CLI는 개인 `bravo-personal` 환경으로 고정하며 운영 워크로드는 별도 T7 자격 어댑터를 사용해야 한다.

- 명령별 출력 2 MB·15초, 수집 회차 60초의 한도를 둔다. 정리할 때 자신이 만든 프로세스 그룹만 종료한다.
- 환경별 최대 23개 노드·40개 설정 관계, 등록 Service 8개, 선택 Pod/컨테이너 수에도 한도가 있다.
  초과 자료를 조용히 잘라 성공으로 표시하지 않는다.
- Service 선택 범위와 클러스터 UID를 고정하고, 현재 등록 digest·생산자 SHA·코드 hash가 맞아야 캐시에 넣는다.
- 관측 시각은 실제 수집 시작 시각이며 최대 유효 시간은 90초다. HTTP 조회·재접속이 이 시각을 연장하지 않는다.
- 수집 실패 시 현재 HTTP·준비 상태를 철회한다. 마지막 자료는 과거로 구분하며 빈 성공이나 보안 통과를 만들지 않는다.
- 브라우저의 시계 검사도 독립 수행한다. 조회 실패·세션 만료는 이전 화면 자료를 제거한다.

CLI의 보호된 등록 파일은 `registration`, 선택 `kubectl`, `producer`를 가진다. `registration`은
`tenant_id`, `environment_id`, `lane`, `cluster_uid`, `namespace`, `services`이며 Service는
`name`, 공개 `label`, `kind`, 선택 HTTP 탐침용 `probe_port`를 가진다.
`producer`는 검증한 전체 `revision`과 현재 수집기 파일의 `sha256`이다. 임의 SHA 문자열은 등록 권한이 아니다.
실제 대상·자격·발행 근거를 확인한 운영자만 이 파일을 준비한다.
T10 환경 ID는 tenant 안에서만 유일하므로 `tenant_id`가 등록에 들어간다. T10 합성은 `attach(api, authorize, tenant_of=plan_tenant(store))`로 붙이고, 계획의 tenant가 등록과 다르면 관측을 403으로 거부한다.

```sh
python -m bravo_console.kubernetes_workload --registration <보호된-등록파일>
```

HTTP 화면에는 `console-observed-topology.v1`을 사용한다. `plan_connected=false`, `authorizes_execution=false`이며
운영 plan_id나 승인 값을 만들지 않는다. 기존 `validate_graph`/`workloadGraph`가 표시 구조만 검증한다.

## 기존 호스트 연결

`ObservedWorkloads.attach(api, authorize)`는 원래 T10 API와 현재 읽기 권한에 결속한 `PlanWorkload`를 만든다.
실제 계획의 앱·출발/목적지 방향을 확인하고, 원래 T21 조회 경계가 전후로 계획·권한·등록을 다시 확인한다.
등록은 같은 프로세스의 실제 수집 결과만 인증한다. JSON/hash 복사본을 인증하거나 자체 권한을 부여하지 않는다.
이 포트를 기존 `compose_console(..., workload=port)`에 전달한다. 기본 자동 연결은 없다.

독립 `ObservationConsole`은 호스트가 제공한 인증 콜백을 요구하며 loopback 읽기 전용이다.
개발 인수에서는 기존 Sova-test의 개인 로컬 운영자 인증을 재사용한다. 기업 IdP/SSO 또는 T10 승인 연결이 아니다.
운영 배포는 기존 T7 자격·T10 권한·T43 이미지/GitOps 경로에서 별도로 인수해야 한다.

## 완료로 확대하지 않는 것

현재 실제 목적지의 자원·관계와 관리 HTTP 탐침을 표시한다. AWS 출발지의 승인된 현재 접속 경로,
양쪽 관측, 전용 읽기 SA/짧은 자격, 운영 인증, 공식 이미지·발행·배포와 T28 통합은 남아 있다.
실제 공격 탐지·격리·복구·컷오버는 각각 원래 공급자와 승인·실행·독립 검증 근거가 필요하다.
관측용 조회 성공만으로 T14/T19 PASS, T21 Done 또는 무중단 이전을 기록하지 않는다.
