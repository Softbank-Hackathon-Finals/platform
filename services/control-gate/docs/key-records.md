# 계획별 출처 공개키 등록·폐기

`PlanAuthorityKeys`는 기존 T10 `plan_commands`와 계획 잠금을 사용해 공개키의 등록과
폐기를 보존한다. 새 DB·테이블·HTTP 등록 서버·서명 서비스는 만들지 않는다.
개인 키를 받는 인자는 없다. T10 호스트가 검증한 주체와 현재 권한을 연결하는
선택적 내부 포트이며, 설치만으로 운영 발급자나 권한이 등록되지 않는다.

## 권한과 범위

- 등록·폐기는 별도 `gate:manage-provenance` 권한을 요구한다. 원문 저장/조회 권한을
  이 권한으로 바꾸지 않는다. 서명 주체 자신의 키 등록도 거부한다.
- 키는 한 계획·tenant·계획 digest, 한 발급자/키 ID, 한 용도와 정확한 생산자 subject에
  결속한다. 역할 이름이나 요청 JSON의 문자열이 인증 근거가 되는 것은 아니다.
- 공개키 등록은 키 revision 0(미등록)에서 1로 진행한다. 이 revision은 T10 계획 버전과
  다르다. T10 계획 등록은 버전 1부터 시작하며 이 포트가 계획 상태/버전을 바꾸지 않는다.
- 같은 관리 주체의 같은 등록 재시도는 원래 영수증을 돌려준다. 다른 내용은 충돌이며,
  같은 키 ID를 다른 키로 덮거나 폐기 후 재활성화할 수 없다.
- 폐기는 revision 2를 추가한다. 이전 등록 행을 UPDATE/DELETE하지 않는다. 폐기 재시도는
  같은 폐기 기록을 읽는다. 새 키로 교체할 때에는 새 키 ID·새 공개키를 사용한다.
  폐기한 공개키를 다른 이름으로 다시 등록하는 것도 거부한다.

기존 기록을 읽을 때도 필드·계획 범위·등록/폐기 이력을 다시 검사한다. 신뢰 시작 시각은
키의 `valid_from`과 서버가 기록한 `enrolled_at` 중 늦은 값이다. 과거 시각을 입력해
등록 전 서명을 신뢰하게 만들지 않는다. 관측 자체의 수집 시각 검증은 서명 발행·소비
경로에서 별도로 수행해야 하며 이 공개키 포트만으로 수집기를 자격화하지 않는다.

## 기존 검증기에 연결

```python
from datetime import UTC, datetime

from bravo_gate.key_records import PlanAuthorityKeys
from bravo_gate.signatures import Ed25519Authority

clock = lambda: datetime.now(UTC)
keys = PlanAuthorityKeys(gate_records, clock=clock)

def authority_for_current_view(view):
    return Ed25519Authority(
        envelope_provider=lambda ctx, subject, purpose:
            read_registered_envelope(view, ctx, subject, purpose),
        key_provider=lambda issuer, key_id: keys.read_key(view, issuer, key_id),
        clock=clock,
    )
```

`view`는 같은 `GateRecords` 객체가 현재 계획 트랜잭션에서 만든 것이다. 다른 정책의
view나 종료된 트랜잭션의 view를 재사용하지 않는다. 등록·폐기 writer도 같은 계획 잠금을
따라야 한다. 조회 중 권한 회수·문맥 변경·저장소 오류가 있으면 과거 공개키로 대체하지 않는다.

`read_key`가 돌려주는 것은 공개 신뢰 재료다. 객체의 존재만으로 권한을 주지 않는다.
`Ed25519Authority.verify`가 목적·서명·현재 폐기·기한을 검사하고, 반환한
`VerifiedAuthority`의 기한을 소비자가 보존해야 한다. 예제의 원문 서명 공급자는 명시적
미연결 지점이며 클라이언트가 제출한 서명을 정본으로 등록하는 콜백이 아니다.

## 인수 조건과 한계

운영 호스트가 실제 관리/생산자 신원, 별도 현재 권한, 공개키 인수 근거와 감사 수집을
연결해야 한다. 개인 키 발급·보관·회전과 승인 절차, 발급자 자격 확인, T14 원문 수집·서명,
T13/예외/네트워크 서명 저장은 이 첫 변경에 포함하지 않는다. 기존 운영 권한을 넓히거나
개발자 개인 키를 운영 발급자로 등록하지 않았다.

계획당 공개키 64개, 기록당 32 KiB로 읽기/등록 비용을 제한한다. 키 이력을 지워 제한을
우회하지 않는다. 장기 계획의 추가 회전이 이 한도를 넘으면 별도 버전 설계가 필요하다.
DB 관리자·신뢰 호스트 침해까지 이 라이브러리만으로 방어하거나 규제 인증을 충족한다고
주장하지 않는다. T19 전체의 운영 완료 체크는 별도다.

[코드·실제 DB·변이·Linux 패키지 검증](../../../docs/evidence/t19-plan-key-records-2026-11-06/README.md).
