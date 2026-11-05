# T15 → T10 검토용 예시

아래는 합성 값이다. 실제 검증 영수증·승인 또는 확정된 전송 규격이 아니다. T10은 발신자를 인증하고 별도로 검증 원문과 산출물을 확인한다. `evidence_ref`는 운영 참조 형식이 합의되기 전 사용하는 가상 식별자다. OIDC 발신자 후보는 **T15 전용 워크로드 신원**이며 issuer·audience·subject는 배포 구성에서 확정한다. 모델이나 Job에 T10 수신 권한을 주지 않는다.

## 정상 후보

```json
{
  "plan_id": "plan-fixture",
  "run_id": "run-fixture",
  "attempt": 1,
  "verification_id": "verification-fixture-1",
  "evidence_ref": "evidence-fixture-1",
  "proposal_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "diff_hash": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "image_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "target_environment_id": "env-onprem",
  "namespace": "rehearsal-fixture",
  "namespace_uid": "fixture-uid-1",
  "cleanup_uid": "fixture-uid-1"
}
```

## 같은 검증의 보안 hold

권한 있는 hold 주체가 `plan-fixture`와 동일한 `verification-fixture-1`에 hold를 기록한 상황이다. 표현은 실제 API 요청이 아닌 경합 시험 조건이다. T10이 같은 계획 잠금에서 hold와 revision을 반영한 뒤에는 위 후보에 대한 새로운 승인·반영 접수를 거부한다. 이미 커밋된 반영 접수의 동일 요청 재조회는 기존 `application_id`만 돌려준다. hold 입력 방식과 권한은 T10 원문을 따른다.

## 수신·실행 경계

- 동일 ID·동일 내용 재전송은 멱등 처리하고 동일 ID·다른 내용은 거부한다.
- T15의 후보 데이터는 `CurrentChange`가 아니다. T10이 신원, 인증된 검증 원문, diff·이미지·대상, 해당 UID의 정리와 취소·hold를 독립 확인해야 한다.
- T10 측 수신·저장·계획 잠금 및 경합 시험은 T10 담당 범위다. T15는 검증 가능한 원문 참조와 발신 신원을 제공한다.
