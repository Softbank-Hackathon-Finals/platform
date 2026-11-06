# T33 Security Hub 스코프·실패 저장 확인

2026-11-06 11:53 KST. 팀 AWS의 등록된 출발지 계정·서울 리전에서
기존 개인 SSO의 STS 신원을 대조한 뒤 신규 명시 수집 경로를 실행했다.
서비스 활성화·샘플 생성·IAM 변경은 수행하지 않았다.

| 검증 | 환경 | 결과 |
|---|---|---|
| STS 기대 계정 대조 → DescribeHub | 실제 AWS | InvalidAccessException 401 |
| 제품 오류 분류 → SQLite 저장 → 화면용 SourceStatus | 실제 SDK + 로컬 저장·조회 모델 | failed=1, ACCESS_DENIED, FAILED |
| 마지막 성공·증분 커서 | 같은 저장소 | 생성/전진 없음 |
| 계정·리전 필터, ASFF 페이지, Hub 전후 대조 | SDK Stubber/모의 응답 | 성공·거부 계약 시험 통과 |
| 실제 양성 탐지 → 운영 조회 API·콘솔 | 미검증 | 서비스 활성화와 운영 배포 인수 필요 |

검증 영수증 SHA-256:
`eac892b41af73e716bc077c54f8e77962278d4e72c5023db3a13ed02cbf36a03`.
원본과 로컬 DB는 비공개 검증 폴더에 보존했다. 계정 식별자·SDK 오류 원문·
자격증명은 공개 증빙에 포함하지 않았다.

첫 실행은 검증 스크립트가 Store를 context manager로 취급하여 수집 전에
중단됐다. Store의 명시 close로 수정한 뒤 위 실행을 완료했다. 제품 실패를
재시도로 숨긴 것이 아니며 최초 실행을 AWS 수집 성공으로 세지 않는다.

오류 원문에 미구독 설명이 있더라도 [DescribeHub 공식 계약](https://docs.aws.amazon.com/securityhub/1.0/APIReference/API_DescribeHub.html)의
InvalidAccessException은 접근 권한 부족을 뜻한다. 오류 코드 하나로 서비스
비활성이나 탐지 부재를 확정하지 않고 ACCESS_DENIED/FAILED로 남긴다.
GuardDuty의 성공한 빈 ListDetectors와는 증거 의미가 다르다.

[GetFindings](https://docs.aws.amazon.com/securityhub/1.0/APIReference/API_GetFindings.html)는
집계 리전에서 다른 리전 결과도 반환할 수 있다. 기대 계정·리전 필터와 행별 대조를
같이 적용한다. 한 번의 조회 성공은 모든 보안 표준·탐지 상품의 활성이나 전역
보안 통과를 뜻하지 않는다. 기존 T33 체크리스트의 실제 탐지 통합 완료로 세지 않는다.
