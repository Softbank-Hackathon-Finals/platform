# CNPG 복원 실패 자격 시험

`verify_restore_failure.py`는 운영자가 별도로 만든 **폐기 가능한 시험 복원본**을 읽는다. 잘못된 백업 ID, 신뢰하지 않는 CA, 잘못된 저장소 자격 또는 최초 복구 WAL 누락을 주입한 복원이 실제로 거부되는지 확인하는 도구다. 백업·Secret·ObjectStore·Cluster를 생성하거나 수정하지 않는다.

이 결과는 DR-01의 PASS도, 복원 성공도 아니다. DR-01.1에는 정상 백업을 새 클러스터로 복원하고 표식 행·행 수를 확인하는 별도 시험이 필요하다. 로컬 kind 결과는 팀 AWS/RKE2의 자격을 대신하지 않는다.

## 준비와 권한 경계

- 운영자가 승인한 시험 클러스터에서만 실행한다. 원본 Cluster나 기존 ObjectStore/Secret은 수정하지 않는다.
- 결함별로 새로운 이름의 Cluster를 만들고, CA/자격을 바꿀 때에는 별도 ObjectStore 및 별도 시험 Secret을 사용한다. 실제 자격을 잘못된 값으로 덮어쓰지 않는다.
- 생성 응답의 Cluster UID를 비공개로 보관한다. API 기본값을 포함한 시험 Cluster spec을 검토하고 SHA-256을 기록한다. `json.dumps(spec, sort_keys=True, separators=(",", ":"))`의 UTF-8 바이트를 해시한다.
- 검증 도구에는 운영자만 관리하는 kubeconfig·kubectl 경로와 위 식별자를 넘긴다. 앱·에이전트가 제공한 UID/해시를 승인 근거로 사용하지 않는다. kubeconfig의 exec 플러그인도 신뢰 경계에 포함된다.
- 필요한 읽기는 해당 Cluster, 복원 Job/Pod, Pod 로그뿐이다. 로그의 원문에는 내부 경로가 있을 수 있어 비공개로 보관한다.

```sh
python qualification/verify_restore_failure.py \
  --kubectl "$KUBECTL" --kubeconfig "$TRIAL_KUBECONFIG" \
  --namespace "$TRIAL_NAMESPACE" --case missing-backup \
  --cluster "$TRIAL_CLUSTER" --cluster-uid "$CREATED_CLUSTER_UID" \
  --expected-spec-sha256 "$REVIEWED_SPEC_SHA256" --backup-id 19990101T000000
```

다른 두 case는 `wrong-ca`, `wrong-credentials`이며, 정상 백업 ID를 고정한 상태에서 주입한 결함만 바꾼다. 존재하지 않는 백업을 사용하면 CA/자격 시험의 대조군이 성립하지 않는다. 같은 저장소에서 정상 복원한 대조군과 원본의 건강 상태도 시험 전후 확인한다.

## 확인 순서

1. Cluster의 UID·spec·백업 ID와 Ready가 아님을 확인한다.
2. Pod → Job → Cluster의 controller 소유권 UID를 대조한다.
3. 같은 Pod의 `full-recovery` 프로세스가 `Error`·0이 아닌 코드로 종료됐는지 확인한다.
4. JSON 오류 레코드의 원인 필드에서 해당 백업 ID, 인증서 검증 실패, 저장소 접근 거부를 찾는다. 정보성 메시지·중복 JSON 키·일반 오류·타임아웃은 근거가 아니다.
5. Pod와 Cluster를 다시 읽어 교체·설정 변경·Ready 전환을 거부한다.

전체 읽기에는 120초 한도, 명령별 15초 한도를 둔다. 16개를 초과하는 Pod·과대 응답·권한 부족·원인 불명은 종료 코드 2와 `UNKNOWN`으로 남긴다. 종료 코드 0은 **주입한 실패가 확인됨**만 뜻한다. 원문 로그나 자격을 표준 출력에 내보내지 않는다.

시험 후에는 생성 영수증의 UID를 서버 `DeleteOptions.preconditions.uid`에 결속해 시험 자원만 정리한다. 정리는 이 읽기 전용 도구의 기능이 아니다. 기존 원본·백업·저장소와 다른 세션 자원은 보존한다.

## 최초 복구 WAL 누락

`missing-wal`은 정상 완료한 백업의 base 파일과 `backup.info`만 **새 시험 prefix**에 복사하고
WAL을 복사하지 않은 경우를 검증한다. 원본 archive에서 WAL을 삭제하거나 보존 정책을
완화하지 않는다. 동일 백업의 정상 복원본과 원본의 Ready·UID·spec, 원본 객체 ETag·크기를
전후 대조한다. 운영 데이터에서는 별도 승인·복사 및 보존 정책 검토가 먼저 필요하다.

`--backup-id`와 함께 `--wal-segment`에 신뢰된 원본 `backup.info`의 `begin_wal`을 전달한다.
운영자가 메타데이터 원본과 hash를 비공개로 보존해야 하며, 에러 로그에서 임의 문자열을
추출해 기대값으로 삼으면 안 된다. 24자리 대문자 16진 WAL 이름만 허용한다.

실제 CNPG/Barman의 `first needed WAL ... WAL not found` 오류가 그 세그먼트를 가리키고,
동일 복원 Cluster가 소유한 recovery 프로세스가 실패해야 해당 결함이 확인된다. 다른 WAL,
미래 WAL을 기다리는 메시지, 일반 timeout·권한 오류는 이 자격을 통과하지 못한다.
이 시험은 **백업 시작에 필요한 WAL 누락**에 한정되며 중간 WAL 손상·PITR 전체를 입증하지 않는다.
