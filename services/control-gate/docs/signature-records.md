# 계획별 서명 공개·재조회 연결

`PlanAuthorityRecords`는 기존 T19 `AuthorityEnvelope`와 `Ed25519Authority`를
T10 계획 트랜잭션에 연결한다. 키 등록·폐기는 `PlanAuthorityKeys`를 그대로 사용한다.
새 서명 규격·서명 서비스·HTTP 업로드 서버·운영 기본 권한을 만들지 않는다.
개인 키를 받거나 서명하는 코드가 아니다.

## 생산과 소비

1. 호스트가 생산자의 실제 신원을 인증하고 현재 계획의 `gate:publish-provenance`와
   `gate:read` 권한을 확인한다. 키 관리 권한과 별개다.
2. 생산자는 이미 인수된 자기 키·단일 용도로 서명한 기존 `AuthorityEnvelope`를 전달한다.
   등록된 producer subject와 현재 요청 주체가 같아야 한다.
3. 현재 계획의 context, 양쪽 T14 원문 또는 저장 판정의 digest와 관측 시각을 다시 읽는다.
   단순히 서명이 유효하다는 이유로 다른 원문·과거 관측을 채택하지 않는다.
4. 같은 계획 잠금 아래 `plan_commands`에 추가하고 저장 직후 서명·기한·권한을 재확인한다.
   같은 서명의 재시도는 원래 기록을 돌려주며 다른 서명으로 덮지 않는다.
5. `authority(view)`가 반환한 기존 Ed25519 검증기는 같은 DB 연결에서 공개키·폐기·서명을
   다시 읽는다. `verify`의 결과와 유효기한을 소비해야 하며 객체 존재만으로 허가하지 않는다.

### 소급 인증을 막는 기준

수집 시작은 `max(서버의 enrolled_at, 키 valid_from)` 이후여야 한다. 수집 종료는
서명 발급 시각보다 늦을 수 없다. 외부 준비 관측도 실제 관측 시각을 제공해야 하며
조회 시각으로 대체하지 않는다. 과거 T14 원문을 새 키로 서명해서 운영 근거로
승격할 수 없다. 이미 보존된 원문의 run ID·바이트·시간·hash는 바꾸지 않는다.
등록 전의 기록은 역사적 증빙으로 보존하고, 운영 소비에는 등록 후 새 수집이 필요하다.

## 연결 예시

```python
from bravo_gate.authority_records import PlanAuthorityRecords
from bravo_gate.stored import StoredGateServices

provenance = PlanAuthorityRecords(
    gate_records,
    clock=server_clock,
    external_subject_provider=read_current_external_subject_metadata,
)
factory = StoredGateServices(
    authority_provider=provenance.authority,
    authorize=check_current_gate_permission,
    approval_provider=read_current_exception,
    binding_provider=read_registered_readiness_binding,
    snapshot_provider=read_registered_readiness_snapshot,
    clock=server_clock,
    network_verifier=verify_registered_network_proofs,
)
```

`read_current_external_subject_metadata(view, purpose, subject_digest)`는 T13 준비 기록·
예외·네트워크 원문의 **인증된 정본 메타데이터**를 같은 view에서 읽어 정확한
`AuthoritySubject(context_digest, subject_digest, purpose, observed_at)`를 반환한다.
미연결·누락·다른 범위·bool/dict 반환은 거부한다. 외부 요청의 주장을 이 타입으로
감싼다고 인증되는 것은 아니다. 현재 서비스가 실제 준비 관측을 검증하는 책임도 유지된다.

T21의 같은 트랜잭션 보고서 조회에는 `authority_factory=provenance.authority`를 연결한다.
새 어댑터 인스턴스에서도 저장된 근거를 읽고 현재 폐기를 반영하며 프로세스 캐시에
신뢰하지 않는다. 트랜잭션이 끝난 view/검증기를 다음 요청에 재사용하지 않는다.

## 운영 조립과 운영자 발급자 (2026-11-07)

운영 조립은 `bravo_platform_events.gate_services`와 `gate_readiness`가 한다. 운영자 승인 호스트·Edge SHIFT 포트는
`"gate": "bravo_platform_events.gate_services:create"`, 워크플로 호스트 GATE는 `service_factory`·`load_gate`를 쓴다.

**서명 주체(T19 결정):** 해피패스까지는 운영자 발급자 하나가 용도별 키를 보관한다. 운영자가 T14 수집·T13 캡처·
GATE 보고서를 확인한 뒤 자기 기기에서 서명하고, 플랫폼에는 공개키와 서명한 봉투만 보낸다. 워크플로 호스트는
검증만 하고 개인 키를 갖지 않는다. 키 등록(`gate:manage-provenance`)과 게시(`gate:publish-provenance`)는 다른
주체다. 해피패스 뒤에는 생산자별 키(T14·T13 Job)로 나눌 수 있게 처음부터 용도별로 등록한다.

**순서:** `operations/gate/run.py`

1. `keys --dir <0700 비공개 경로>`: 용도별 키를 만든다.
2. `enroll`: 수집보다 **먼저** 공개키를 등록한다. 등록 전에 시작한 수집은 서명할 수 없다. 처음 정한 유효기간을
   `--dir`에 남기므로 일부만 등록된 뒤 다시 실행해도 충돌하지 않는다. 키 유효기간은 48시간 이하다.
3. T14 실행 기록은 기존 수집 경로(`gate_record_host`)로 남긴다. 반영 뒤 컨텍스트 결속은 워크플로 호스트의
   `post_apply_prepare`(#565)가 한다. relay에는 이 단계의 도구가 없다. T13 기록은 `readiness --binding … --snapshot …`.
4. `subjects`로 서명할 지문과 이미 게시된 용도를 확인하고, 출력의 `confirm`을 `sign --confirm <값>`에 넘긴다.
   기본 `sign`은 보여 준 것 중 아직 게시되지 않은 용도만 서명한다. 확인 뒤에 T13 기록이나 GATE 판정이 새로
   생기면 `confirm`이 달라져 `sign`이 거부한다. 다시 `subjects`로 확인한다.
5. gate-result 서명은 해피패스에 필요하지 않다. 워크플로 GATE reader는 SHIFT·컷오버처럼 같은 트랜잭션의 플랫폼
   재평가(`served_reevaluation`)를 소비한다. 입력 5종(context, run 2종, readiness 2종)의 서명은 그대로 필요하다.

**readiness 기록의 신뢰 근거:** relay는 호출자가 보낸 T13 binding·snapshot을 그대로 저장한다. 신뢰 근거는
운영자의 readiness-binding·readiness-snapshot 서명뿐이다. GATE 평가부터 SHIFT 사이에 readiness를 새로
기록하면 가장 새 기록이 쓰이므로, 그 기록에 다시 서명하기 전까지 소비가 거부된다
(`COMBINED_GATE_CONTEXT_MISMATCH` 등).

**relay 권한:** relay(`gate_relay`)는 워크플로 호스트 안에서 돈다. 누가 행동하는지는 호스트 설정
`/workflow/gate-relay.json`에 고정되고, 요청이 고를 수 없다. 세 주체는 OperatorAccess 권한과 기록의 주체를 나눌
뿐, exec 가진 사람을 나누지 않는다. **t10-workflow-host의 `pods/exec` 권한은 게이트 전권**(admin·issuer·operator)이다.

## 운영 인수에서 남은 것

운영 관리·생산자 인증, 공개키 자격, 개인 키의 발급·보관·회전, T14 수집기의 실제
신뢰 경계와 서명, T13/예외/네트워크 정본 메타데이터 공급자 및 감사 수집은 호스트가
명시적으로 연결해야 한다. 현재 원문 저장 권한을 서명·평가 권한으로 바꾸지 않는다.
계획 상태/버전·승인·업무 쓰기·트래픽을 이 포트가 변경하지 않는다.

동일 context/subject/purpose에 새 기한의 서명으로 덮어쓰지 않는다. 기한이 지나면
새 관측과 새로운 context를 준비해야 한다. 계획당 서명 4,096건·행당 32 KiB로 제한하고
한도 도달 시 기존 근거를 지워서 공간을 만들지 않는다. DB 관리자 또는 신뢰 호스트
침해를 이 라이브러리 하나로 막거나 금융 규제 인증을 충족한다고 주장하지 않는다.
