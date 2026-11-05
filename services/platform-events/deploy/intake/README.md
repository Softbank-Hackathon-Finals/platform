# T15 발신 신원과 T10 수신 설정

`intake_deployment`는 배포 설정과 공개 JWKS를 검증하고, 기존 trusted host
Deployment에 적용할 token projection patch를 만든다. 자원 생성·토큰 발급·외부
키 조회를 수행하지 않는다. 실제 클러스터의 issuer·키·tenant를 확인하기 전에는
`binding.example.json`을 적용하지 않는다.

## 설정과 토큰

1. 인프라 담당이 TLS를 검증한 Kubernetes API에서 issuer와 공개 JWKS를 읽는다.
   실제 issuer는 `/.well-known/openid-configuration`, 공개 키는 `/openid/v1/jwks`로
   확인한다. 수신 토큰의 `jku`·`x5u`나 임의 URL을 조회하지 않는다.
2. `binding.example.json`에 현재 issuer, 등록 tenant, 검토한 `kid`를 채운다.
   `namespace`와 `service_account`는 전용 trusted host를 가리킨다.
   audience는 `bravo-t10-rehearsal-intake`로 고정한다.
3. T10에 설정 JSON과 공개 JWKS를 읽기 전용으로 제공한다. private RSA 파라미터,
   중복 kid, 다른 알고리즘, 다른 audience는 시작 전에 거부한다.
4. 기존 host manifest의 실제 Deployment·container 이름으로 아래 patch를 만든다.
   배포 소유자가 최종 manifest를 검토한 뒤 합의한 작업창에서 적용한다.

```python
import json
from pathlib import Path
from bravo_platform_events.intake_deployment import sender_from_config, sender_pod_patch

config = json.loads(Path("binding.json").read_text())
jwks = json.loads(Path("public-jwks.json").read_text())
sender = sender_from_config(config, jwks)
patch = sender_pod_patch(config, jwks, deployment="trusted-host", container="host")
Path("host-token.patch.json").write_text(json.dumps(patch, indent=2) + "\n")
```

출력은 strategic merge patch다. 이미지·실행 명령·TLS 경로·NetworkPolicy를 포함한
완전한 Deployment가 아니다. 토큰 volume은 지정한 trusted host container만 읽는다.
모델 worker는 별도 Pod에 둔다. 같은 Pod의 신뢰하지 않는 container에 이 mount를
추가하지 않는다. 기본 API 토큰의 자동 mount는 끈다. 대상 Kubernetes API용
`rehearsal-exec`·`apply-deployer` 자격은 다른 전달 경로를 유지한다.

projection은 600초를 요청한다. T15의 `submit_candidate`에는
`token_supplier=ProjectedToken()`을 연결한다. 공급자는 전송할 때마다 파일을
다시 읽어 kubelet의 교체를 반영한다. 토큰을 환경변수·설정 파일·로그에 복사하지 않는다.
수신기는 실제 `exp - iat`가 1시간을 넘으면 거부한다. 서버가 요청한 수명과 다른
토큰을 발급하면 검증을 약하게 바꾸지 않고 운영 설정을 확인한다.

고정 공개 키는 RS256 서명을 검증한다. `kid` header를 받지만 토큰의 kid로
새 키를 선택하지 않는다. 키 교체 시 검토한 JWKS·key_id와 호스트를 함께 갱신한다.
이 오프라인 검증은 Pod·SA 삭제를 즉시 확인하지 않으며 유효기간까지 토큰을
인정할 수 있다. 즉시 철회가 필요하면 별도 TokenReview 경로를 먼저 인수해야 한다.
이 설정은 TokenReview 권한을 새로 부여하지 않는다.

## 원문과 hold 연결

`ChangeIntake(store, sender=sender, read_original=reader, authorize_hold=policy)`에
실제 reader와 관리 정책을 명시해서 넣는다. 둘 다 없으면 호스트를 열지 않는다.
`create_operator_cutover_app(..., change_intake=intake)`는 같은 store를 사용한다.

- T15 `AttemptStore`의 HMAC 키는 trusted runner만 보유한다. T10 JWT 공개 키와
  다르다. 모델·worker·후보 요청으로 키나 PASS를 전달하지 않는다.
- 기존 trusted host 안의 `RecordedEvidence.read_original`을 사용할 수 있다.
  별도 T10 프로세스로 옮길 때는 T15 담당이 인증 원문 조회 접점을 인수해야 한다.
  공유 PVC의 read-only mount만으로 HMAC 검증이 완성되지 않는다.
  T10에 HMAC 키를 복제하는 PVC·Secret 설정은 이 artifact에 없다.
- hold는 후보 발신자에게 부여하지 않는다. 취소·보안 관리 주체를 별도로 정하고,
  현재 계획·검증 ID·결정 참조의 권한을 같은 DB 잠금 안에서 확인한다.
  `authorize_hold=lambda *_: True`나 workload subject를 사람 승인자로 사용하는
  설정은 운영 연결이 아니다. 후보 HTTP에는 hold 경로가 없다.

원문 조회·hold 주체·실제 T15/T38/T31 연결이 확정되지 않으면 수신 경로를 열지
않는다. 이 artifact의 합성 RSA 시험은 실제 발급·배포·S2/S3 완료 증거가 아니다.
완료 판단은 [T10 승인 계약](../../../../docs/contracts/T10-change-approval.md)을 따른다.

공식 참고: [ServiceAccount token projection과 issuer discovery](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/),
[오프라인 검증과 TokenReview의 철회 차이](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/#verifying-and-inspecting-private-claims).
