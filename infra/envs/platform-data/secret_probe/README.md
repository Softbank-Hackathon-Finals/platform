# T7 단일 Secret 접근 시험

플랫폼 운영자가 승인받은 전용 검증 namespace에서 같은 빈 Secret을 세 번 읽는다. 첫 번째와 마지막 Job은 `reader`, 가운데 Job은 `denied` 서비스 계정으로 실행한다. 세 Job은 각각 10분짜리 Pod 결속 토큰을 명시적으로 투영한다. 두 서비스 계정 모두 토큰을 자동 마운트하지 않는다. 이 시험이 생성하는 RBAC 권한은 reader의 `t7-synthetic` 단건 GET 하나다.

**실행 전.** 신뢰하는 kubeconfig와 대상 클러스터를 확인한다. 운영자가 새 시험 namespace `t7-secret-probe-<무작위 12자리 소문자 hex>`를 생성하고 UID를 기록한다. 그 namespace에는 운영 워크로드나 외부 주체의 생성 권한이 없어야 한다. 파드는 Restricted 보안 정책으로 실행한다. 감사 담당자는 해당 클러스터에서 두 시험 계정의 Secret GET을 `Metadata` 수준의 `ResponseComplete` 이벤트로 조회할 수 있어야 한다. 메타데이터에서 서비스 계정·Pod UID, API 서버 권한 결정, 상태 코드를 읽는다.

1. 해당 API Service와 API 서버 backend의 IP·포트를 운영자에게 확인한다. CNI의 Service 주소 변환 전후에 필요한 주소만 각각 `--api-peer`로 준다. 정책은 시험 Job의 이 목적지만 연다. 승인된 환경에서 이미지 digest의 pull을 확인한다.
2. 소유자 전용 디렉터리에서 아래 명령을 실행한다. `--receipt`에는 존재하지 않는 경로를 쓴다. Job이 읽는 메타데이터에는 Secret 값이 없다. 거부 응답의 본문은 읽지 않는다.
3. 감사 담당자가 **같은 API 서버**의 JSONL을 읽어 판정한다. 판정기에 신뢰할 수 있는 영수증·감사 원본을 직접 건넨다. 세 요청의 Audit-ID, 사용자와 Pod UID, 요청 종류·대상과 판정 코드가 모두 일치해야 한다.
4. 실행 담당자는 영수증의 `cleanup_confirmed`와 보조 자원 부재를 확인한다. 실행을 중단했다면 동일한 kubeconfig·namespace·UID·영수증에 `--cleanup-only`를 사용한다. UID가 바뀌었거나 생성 응답이 유실된 객체는 담당자가 원본을 대조한다. namespace를 만든 담당자가 마지막에 원 UID로 namespace를 삭제하고 부재를 확인한다.

```sh
python3 run.py --kubeconfig "$KUBECONFIG" --namespace "$PROBE_NS" --namespace-uid "$NS_UID" \
  --api-peer "$API_SERVICE_IP_PORT" --api-peer "$API_BACKEND_IP_PORT" --receipt "$PRIVATE_RECEIPT"
python3 assess.py "$PRIVATE_RECEIPT" "$TRUSTED_AUDIT_JSONL"
```

`PASS`는 정리가 끝나고 같은 Secret UID에서 reader 200, 다른 계정 403, reader 200을 확인한 경우다. 같은 대조에서 다른 계정도 200이면 `FAIL`이다. 누락·중복 감사, 잘못된 UID, 401·404·전송 오류, 정리 불명 등은 `UNKNOWN`이다. 종료 코드는 순서대로 0, 1, 2다.

이 시험은 지정한 클러스터의 **합성 자원 두 계정**에 대한 근거를 제공한다. T15 제품 검증에서 실제 앱의 OpenBao·ESO 자격 전달, 다른 앱 계정의 접근 거부, T38의 시도별 정리까지 인수하려면 제품 대상과 실행 신원을 별도로 결속해야 한다. [구독 실행 기반과 제품 검증 범위](../../../../operations/subscription/README.md)
