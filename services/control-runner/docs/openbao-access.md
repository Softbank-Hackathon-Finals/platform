# OpenBao 접근 시험 — SEC-01.1

T14가 제공하는 신뢰된 서버 내부 수집기다. T9의 OpenBao 설치·인증 역할·정책·ESO를
생성하거나 변경하지 않는다. `openbao.OpenBaoSecretAccessProbe`를 등록하면 T5의
`SEC-01.1 / method_version=1` 결과를 기존 `Runner`가 생성한다.

## 서버가 등록해야 할 입력

| 입력 | 검증 책임 |
|---|---|
| 앱과 `Environment` | 요청 사용자의 조직·앱 소유권, 등록된 목적지 클러스터를 호출 서버가 확인 |
| HTTPS origin, CA PEM | 운영자가 승인한 OpenBao 서버와 CA. 업로드된 URL/인증서로 대체 금지 |
| KV-v2 mount·path·현재 version | 해당 앱 DB Secret이며 같은 등록 revision에 속하는지 확인 |
| 앱 SA와 다른 SA의 OpenBao 토큰 | 서버가 각각 Kubernetes 인증을 수행하고 반환된 accessor를 결속 |
| auth mount, namespace, SA 이름·UID, role | 해당 auth mount가 등록 클러스터를 검증하고 정확한 SA에 역할을 부여하는지 확인 |
| evidence sink | 접근 통제된 보존 경로. 저장 실패 시 결과를 성공으로 반환하지 않음 |

SA 문자열만 받거나 앱이 제공한 토큰·결과 JSON을 신뢰하면 안 된다. root 및
`auth/token/create`로 만든 토큰은 운영 시험 신원으로 허용하지 않는다. 다른 SA는
이름·UID·토큰 accessor가 앱 SA와 달라야 한다. 동일 auth mount·서버·CA에서 시험한다.
`lookup-self` read 권한은 두 토큰 모두 필요하다. 권한이 없으면 통과하지 않는다.

## 실제 호출 순서

1. 양쪽 토큰으로 `GET /v1/auth/token/lookup-self`.
   accessor, Kubernetes login 경로, SA 이름·namespace·UID·role, 양수 TTL을 확인한다.
2. 앱 토큰으로 `GET /v1/{mount}/data/{path}`.
   정상 응답, 데이터 객체, 등록한 현재 버전, 미삭제·미파괴 상태를 확인한다.
3. 다른 SA 토큰으로 **동일 경로**를 읽는다.
4. 양쪽 신원을 다시 확인하고 앱 토큰으로 같은 현재 버전을 다시 읽는다.
5. 원시 응답 대신 고정 상태·단계·수집 시각·실행 결속과 입력 해시만 보존한다.

`version=N`으로 과거 Secret을 읽지 않는다. 일반 current read의 응답 버전이 등록 값과
다르면 재등록·재평가가 필요하다. 시험 전후 확인은 관찰을 감싸는 방식이며, 중간 정책 변경을
완전히 잠그거나 원자적인 스냅샷을 제공하지 않는다.

| 관찰 | T5 결과 |
|---|---|
| 신원·앱 대조군 전후 성공 + 다른 SA에 OpenBao의 `403 permission denied` | PASS |
| 같은 버전을 다른 SA도 읽음 + 신원·대조군 성공 | FAIL |
| 401·404·503, 다른 403 본문, 버전 변화, 타임아웃·TLS 오류, 잘못된 신원 | UNKNOWN |
| 대조군 실패·미실행 또는 증거 저장 실패 | PASS 불가 |

403 자체는 ACL 거부의 충분한 근거가 아니다. 토큰이 무효여서 생긴 403을 통과시키지
않기 위해 전후 신원 확인이 필요하다. 모든 정책 경쟁 상황을 증명한 것으로 확대하지 않는다.

## 통신·비밀 경계

- 명시한 CA와 hostname을 검증하는 TLS 1.2 이상, HTTPS origin만 사용한다.
- 환경변수 프록시·쿠키·리다이렉트·자동 재시도를 사용하지 않는다.
- 조회 외의 API를 제공하지 않는다. 인증 토큰 생성·회전·정책 쓰기는 이 수집기 범위 밖이다.
- 요청당 socket timeout은 0초 초과 10초 이하, JSON 응답은 1 MiB 이하로 제한한다.
  중복 JSON 키, NaN/Infinity, 비JSON·압축 응답은 거부한다.
- socket timeout은 전체 작업의 절대 제한시간이 아니다. 호출 서버는 별도 실행 작업의
  전체 예산·실제 종료·취소를 강제해야 한다. DNS 지연이나 계속 도착하는 느린 응답까지
  이 값만으로 제어했다고 주장하지 않는다.
- Secret 값·토큰·accessor·서버 오류 문자열·URL을 증거에 넣지 않는다. Secret 값은
  HTTPS/JSON 처리 중 메모리에 존재하므로 wire/debug 로그·메모리 덤프 수집을 금지한다.
  Python 메모리에서 즉시 안전하게 지워진다고 보장하지 않는다.
- 해시는 보존된 관찰의 식별자이며 서명이나 신원 인증을 대체하지 않는다.

## 연결 예시

아래 값들은 HTTP 요청에서 직접 받지 않고 서버가 검증한 등록 레코드와 인증 결과에서 가져온다.

```python
from bravo_controls.openbao import BaoTarget, OpenBaoClient, OpenBaoSecretAccessProbe

probe = OpenBaoSecretAccessProbe(
    BaoTarget(app_id, environment, mount, secret_path, registered_version),
    app_client=OpenBaoClient(origin, ca_pem, app_token, app_principal),
    other_client=OpenBaoClient(origin, ca_pem, other_token, other_principal),
    evidence_sink=retain_evidence,
)
# 기존 T5 Catalog, Runner 생성 경로에서 probes["SEC-01.1"] = probe
```

## 검증 범위와 인계

`tests/test_openbao.py`는 합성 OpenBao 응답과 실제 loopback TLS 연결로 수집기 →
T5 ControlRun 경로를 시험한다. 거부, 잘못된 신원, 만료·회수, 버전 변경, 과도한 응답,
중복 키, 인증서·hostname 오류, 리다이렉트와 증거 저장 실패를 확인한다.

이는 **실제 OpenBao 서버의 정책 또는 팀 RKE2·Kubernetes 인증 시험이 아니다.**
T9 준비 후 아래를 실제 목적지에서 추가 검증해야 한다.

1. 등록 클러스터의 두 SA로 실제 Kubernetes login 및 accessor·UID 결속.
2. 앱의 같은 현재 Secret 읽기 성공, 다른 SA 거부, 다른 SA 토큰 만료 시 UNKNOWN.
3. 해당 Secret을 실제 ESO가 앱 namespace에 전달하는 경로와 정책 확인(T9/T30 담당).
4. 수집기 증거를 보존하고 T19가 실제 source/target run으로 비교하는 통합 시험.

이 PR만으로 T14 전체, ESO 전달, Secret 회전, 양쪽 환경 동등성 또는 금융 보안 적합성을
완료 처리하지 않는다. T14 #21 체크박스는 실제 완료 기준 검증 후 갱신한다.

## 공식 인터페이스 근거

- [OpenBao HTTP API](https://openbao.org/api-docs/): 토큰 헤더와 API 경계.
- [KV-v2 read](https://openbao.org/docs/api/secret/kv/kv-v2/#read-secret-version): current read와 버전 메타데이터.
- [Token lookup-self](https://openbao.org/docs/api/auth/token/#lookup-a-token-self): 토큰 자기 조회.
- [Kubernetes auth](https://openbao.org/docs/auth/kubernetes/): 인증 결과의 SA metadata.
