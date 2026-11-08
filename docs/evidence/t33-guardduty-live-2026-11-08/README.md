# T33 실제 GuardDuty 양성 탐지 수집 · 요약 · 수집 끊김 — 2026-11-08

2026-11-08 04:31–04:35 KST(19:31–19:35 UTC), 팀 출발지 AWS 계정의 서울 리전에서 확인했다. Security Hub는 구독하지 않은 계정이라(`InvalidAccessException`, 11/6 증빙과 같음) 설계의 대안 경로인 **GuardDuty 직접 수집**을 썼다.

## 한 일

1. 운영자가 GuardDuty 탐지기를 켜고(`component=source`, `task=T33` 태그) 샘플 탐지 3종을 만들었다. 3종은 `Policy:S3/BucketBlockPublicAccessDisabled`, `Recon:EC2/PortProbeUnprotectedPort`, `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS`이다. 수집기는 탐지기 · 샘플을 만들지 않는다.
2. `bravo_findings.aws_pull`(읽기 전용: STS, `ListDetectors`, `GetDetector`, `ListFindings`, `GetFindings`)로 비공개 SQLite에 수집했다.
3. 같은 DB로 조회 API(`uvicorn bravo_findings.api:create_app`)를 루프백에 띄우고, 하루짜리 읽기 자격(레지스트리에는 sha256만, 0600)으로 `summary` · `sources` · `findings`를 조회했다.
4. 수집 끊김: 기대 계정을 다른 값으로 주어 수집을 실패시킨 뒤 다시 조회했다.

## 결과

| 단계 | 출처 상태 | `env-aws` 위협(`THREAT`) 칸 | 목록 |
|---|---|---|---|
| 탐지기 켜기 전 | `FAILED` · `NOT_ENABLED` | `UNKNOWN` | 0 |
| 수집(수정 전 코드) | `FAILED` · `SOURCE_UNHEALTHY` | `UNKNOWN` | 0 |
| 수집(수정 뒤 코드) | `OK`, 3건 생성 | `OK`, 심각도 2: 2건 · 4: 1건 | 3건, 모두 `sample: true` |
| 수집 끊김(계정 불일치) | `FAILED` · `ACCESS_DENIED`, 마지막 성공 시각 유지 | **`UNKNOWN`**(건수 유지) | 3건 그대로 |

온프렘 칸(`falco` · `kyverno`)은 아직 수집이 없어 `STALE`로 나왔다. "0건"으로 보이지 않는다.

## 찾은 결함과 수정(이 PR)

**실제 `ListFindings`는 마지막 페이지에 `NextToken: ""`(빈 문자열)을 돌려준다.** 수집기는 빈 토큰을 `invalid_pagination`으로 거부했다. 그래서 탐지가 한 건이라도 있으면 수집이 늘 `SOURCE_UNHEALTHY`였다. 지금까지는 탐지 0건이나 비활성 계정에서만 실측해서 드러나지 않았다. `ListDetectors`는 토큰을 아예 빼므로 영향이 없었다.

- 수정(`guardduty.py`): `None`과 `""`를 마지막 페이지로 본다. 문자열이 아니거나 반복된 토큰은 그대로 `invalid_pagination`이다.
- 시험: `test_live_last_page_ends_with_an_empty_next_token`(Stubber, 실제 응답 모양). 수정을 되돌리면 실패한다. 전체 184 통과.
- Security Hub(`securityhub.py`)도 빈 토큰을 거부한다. 실제 응답 모양은 구독 계정에서 확인하기 전까지 바꾸지 않는다.

## 남은 것 · 정리

- 운영 스케줄러와 플랫폼 배포(인증 API · 콘솔 연결)는 아직이다. 이 실측은 로컬 루프백 API에서 했다.
- 온프렘(Kyverno · Falco)은 목적지 설치 뒤 같은 방식으로 확인한다.
- 탐지기는 켜 둔다(GuardDuty 30일 무료 체험). 데모 뒤 정리: `aws guardduty delete-detector --detector-id <탐지기 ID> --region ap-northeast-2`. 계정 ID와 탐지기 ID는 공개하지 않는다.
