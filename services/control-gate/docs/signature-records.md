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

## 운영 인수에서 남은 것

운영 관리·생산자 인증, 공개키 자격, 개인 키의 발급·보관·회전, T14 수집기의 실제
신뢰 경계와 서명, T13/예외/네트워크 정본 메타데이터 공급자 및 감사 수집은 호스트가
명시적으로 연결해야 한다. 현재 원문 저장 권한을 서명·평가 권한으로 바꾸지 않는다.
계획 상태/버전·승인·업무 쓰기·트래픽을 이 포트가 변경하지 않는다.

동일 context/subject/purpose에 새 기한의 서명으로 덮어쓰지 않는다. 기한이 지나면
새 관측과 새로운 context를 준비해야 한다. 계획당 서명 4,096건·행당 32 KiB로 제한하고
한도 도달 시 기존 근거를 지워서 공간을 만들지 않는다. DB 관리자 또는 신뢰 호스트
침해를 이 라이브러리 하나로 막거나 금융 규제 인증을 충족한다고 주장하지 않는다.
