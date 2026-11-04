# ADR-0016: 목적지 개발 앱의 Secrets Manager 호환 컨테이너

- 상태: 제안 (PR 리뷰와 병합 후 적용)
- 날짜: 2026-11-04
- 범위: T30 #17 개발 목적지 `target-dev`. T15 · T31의 승인 경로를 대신하지 않는다

## 배경

T17 · T8 · T18의 Edge 경유 측정에는 `target-dev`의 앱과 Service가 필요하다. demo-chat은 시작할 때
`app/db_secret.py`에서 Secrets Manager `GetSecretValue`를 호출하고, 실패하면 뜨지 않는다.
이 호출은 S2의 "코드 수정" 시연을 위해 일부러 남겨 둔 의존성이다. 게시본 `8d82a92`에도 그대로 있고,
승인된 수정 이미지는 아직 없다. 목적지에 AWS 자격을 두는 것은 금지다.

## 고려한 옵션

| 옵션 | 판단 |
|---|---|
| 앱을 고쳐 파일이나 환경 변수에서 읽기 | T15 승인 뒤 T31 이미지로 할 일이다. 미리 바꾸면 S2 시연이 사라진다 |
| 진입점 교체 · Python 주입 · 몽키패치 | [이미지 인수인계](../operations/target-image-handoff.md)가 금지한다 |
| 실제 AWS Secrets Manager 호출 | 목적지에 AWS 자격을 둬야 하고 첨부 S3 자격과도 섞인다 |
| 측정 때까지 앱 없이 기다리기 | Edge 경유 측정을 할 수 없다 |
| 같은 Pod의 별도 컨테이너가 루프백에서 응답 | 앱 코드와 진입점을 건드리지 않는다. 대신 출구 증거로 쓸 수 없다 |

## 결정

**같은 이미지로 뜨는 별도 컨테이너가 `127.0.0.1`에서 `GetSecretValue` 하나만 응답한다.**
앱에는 `AWS_ENDPOINT_URL_SECRETS_MANAGER`만 추가한다. 응답 값은 T9 계약 Secret
`target-app-credentials`의 `credentials.json`에서 읽고, 호출마다 파일을 다시 읽는다.

**이 구성은 출구 증거가 아니다.** 앱은 여전히 AWS SDK로 Secrets Manager를 부르고, 응답하는 쪽만 바뀌었다.
Pod에 `bravo.t30/not-exit-evidence` 표시를 남긴다. Exit Drill(T27), 인벤토리(T16), 게이트(T19),
S2 시연의 결과로 세지 않는다. 특히 Exit Drill은 AWS를 막아도 이 구성에서 통과할 수 있으므로,
이 표시가 있는 Pod의 결과는 "앱이 AWS에서 벗어났다"는 근거가 되지 못한다.
**지금은 이 표시를 읽는 코드가 없다.** 이 문장과 PR 설명만 막고 있으니, Exit Drill이 표시를 보도록 고치는 일은 T27의 후속 작업이다.

## 결과

- 측정에 필요한 앱과 Service를 AWS 자격 없이 올릴 수 있다.
- 호환 컨테이너가 쓰는 파일 경로는 T15 제안 경로 `/run/bravo/database/credentials.json`과 같다.
  DB 자격 Secret은 이 컨테이너에만 마운트한다.
- **남는 노출:** 응답기는 서명을 검사하지 않는다. Pod 밖에서 닿는 길은 port-forward뿐이고, `pods/portforward` 권한자는
  `secrets get` 권한 없이 출발지 앱 비밀번호를 받을 수 있다. 저장소에 이 권한을 주는 역할은 없다.
- **없앨 때:** 승인된 T31 이미지가 파일에서 직접 읽게 되면 이 컨테이너, ConfigMap, 앱의 endpoint 변수와
  `not-exit-evidence` 표시를 같은 변경에서 지운다.
- 실제 시험 결과와 대역은 [증거](../evidence/t30-target-app-2026-11-04/README.md)에 적는다.
