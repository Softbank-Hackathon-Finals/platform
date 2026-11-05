# ADR-0027: 평가 프로필을 등록 계획의 digest에 결속

- 상태: Accepted (T10·T14·T19 담당 합의, 구현·실환경 인수는 별도)
- 날짜: 2026-11-06
- 근거: [최신 식별자 결정](https://github.com/Softbank-Hackathon-Finals/platform/issues/29#issuecomment-6661068881), [최종 분담](https://github.com/Softbank-Hackathon-Finals/platform/issues/29#issuecomment-6661407615), [T10 인수](https://github.com/Softbank-Hackathon-Finals/platform/issues/29#issuecomment-6661163853)

## 맥락

실제 등록은 앱에 생성된 `workload_id`를 부여한다. T10은 통제 원문의 `app`을
이 ID와 대조하지만, T14·T19는 카탈로그 프로필의 앱 표시 이름도 같은 값으로
요구했다. 앱 이름과 등록 ID가 같은 시험 자료에서는 이 충돌이 드러나지 않았다.

## 결정

원문·게이트의 `app`은 등록된 `workload_id`다. 평가 프로필은 앱 이름으로 찾지
않고 계획에 명시한 `profile_id`·`profile_version`으로 선택한다. T10 등록 호스트는
tenant별 서버 허용 목록을 확인하고 두 값을 spec·영수증·계획 digest에 함께 저장한다.
게이트 컨텍스트는 이 값과 같아야 하며, 누락 시 닫힌 상태를 유지한다.

T10은 계획 등록·결속을 맡는다. T14 담당은 `(id, version)` 조회를, T19 담당은
앱 이름 대조 제거와 계획의 프로필 대조를 맡는다. T5 카탈로그 바이트는 유지한다.
상세 필드와 인수 절차는 [등록 계약](../contracts/T10-registration.md)에 둔다.

## 대안과 결과

- 앱 이름을 원문 ID로 쓰면 등록 ID와 대상의 연결이 끊기고 tenant 간 이름 중복을
  별도로 처리해야 한다. 채택하지 않는다.
- 같은 계획에 별도 프로필 기록만 붙이는 안은 기존 plan ID를 유지할 수 있지만,
  프로필 선택이 계획 digest에 포함되지 않는다. 담당 합의로 채택하지 않는다.
- 기존 계획의 spec·digest·원문은 수정하지 않는다. 기존 환경·앱이 현재도 유효하면
  이를 재사용해 새 계획을 만든다. 새 계획의 권한·증거는 해당 digest에 다시 결속한다.
- 프로필 저장은 시험 통과나 실행 허가가 아니다. 현재 권한·원문 인증·T13 준비·
  사람 승인과 단일 실행 조건은 각 소비자가 계속 검증한다.
