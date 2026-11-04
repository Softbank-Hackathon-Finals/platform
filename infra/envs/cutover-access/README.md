# Executor 추가 권한

T7의 기본 권한 위에 명시적으로 적용하는 별도 Role이다.
[T12 연결 계약](../../../docs/contracts/T12-platform-integration.md)과
[ADR-0013](../../../docs/adr/0013-executor-workload-access.md)을 먼저 읽는다.
두 클러스터에 각각 적용하며, 모든 파드 이름은 현재 owner/UID/image를 확인한 입력이어야 한다.
기존 broad Role이 있으면 좁은 Role을 추가해도 기존 권한은 줄어들지 않는다.

```sh
python3 infra/envs/cutover-access/render.py \
  --namespace source-dev --pooler-config source-pgbouncer-config \
  --pooler-pod "$POOLER_POD" --app-pod "$APP_POD" > "$PRIVATE_DIR/executor-access.json"
kubectl --kubeconfig "$KUBECONFIG" apply --server-side --dry-run=server \
  -f "$PRIVATE_DIR/executor-access.json"
kubectl --kubeconfig "$KUBECONFIG" apply --server-side \
  -f "$PRIVATE_DIR/executor-access.json"
python3 infra/envs/cutover-access/verify.py \
  --kubeconfig "$KUBECONFIG" --namespace source-dev \
  --pooler-config source-pgbouncer-config --pooler-pod "$POOLER_POD" \
  --pooler-container source-pgbouncer --app-pod "$APP_POD" --db-pod "$DB_POD"
```

앱이 여러 파드면 `--app-pod`를 반복한다. 다른 namespace의 Executor SA는
`--identity-namespace`로 지정한다. 기본 SA는 각 namespace의 `bravo-executor`와
`bravo-collector`이며 T7 배포가 먼저 있어야 한다. 검증자는 TokenRequest 가능한 운영자다.
검증에 발급한 토큰을 파일로 저장하거나 커밋하지 않는다.
본인 토큰 발급 거부는 workload namespace가 아니라 해당 ServiceAccount의 namespace에서 검사한다.

Secret 읽기는 두 SA 각각에 대해 workload namespace와, 다르면 identity namespace에서 검사한다.
일반 `get/list/watch` 거부만으로 이름 제한 권한이 없다고 판단하지 않는다.
SelfSubjectRulesReview로 합산된 규칙의 `resourceNames`를 수집하므로 아직 존재하지 않는
Secret 이름도 포함한다. 각 이름의 `get/list/watch`와 이름 없는 세 요청은
SelfSubjectAccessReview로 조회하며 Secret 값은 가져오지 않는다.
규칙에 Secret 읽기 허용이 남아 있거나 실제 권한 조회가 허용이면 실패한다.
규칙 목록이 불완전하거나 조회·형식 오류가 있으면 통과시키지 않고 운영자 검토가 필요하다.
규칙 목록은 권한 감사용이며 런타임 접근 허가를 대신하지 않는다.
([Kubernetes SelfSubjectRulesReview](https://kubernetes.io/docs/reference/kubernetes-api/definitions/self-subject-rules-review-v1-authorization/))

`secret_read_audit`에는 검사한 namespace, 규칙 완전성, 이름별 검사 수와 허용 응답 수를 남긴다.
이름과 원시 규칙·자격증명은 출력하지 않는다. 통과 범위는 **그 시점의 위 namespace들**이며
클러스터 전체, 다른 namespace, 이후 추가된 권한이나 exec로 접근 가능한 파드 내부 자격증명을
포함하지 않는다. 다른 namespace의 권한은 운영자가 별도로 검토해야 한다.
과거 결과의 `secret_list_denied`만으로 특정 Secret의 `get` 거부까지 주장하지 않는다.

목적지 앱이 아직 배포되지 않은 준비 단계에는 두 명령 모두 `--app-pod` 대신
`--pooler-only`를 명시할 수 있다. 이 모드는 현재 풀러 하나에만 exec를 부여하며
미래 앱 이름을 임의로 넣지 않는다. 검증 결과의 `app_access_verified`는 false이고
앱 권한·차단 확인은 미완료다. 실제 앱 배포 후 전체 현재 파드 목록으로 Role을 다시
렌더링하고 기본 검증을 수행한다. 어떤 모드든 `cutover_ready`는 false이며 전체
네트워크·DB·Executor 컷오버 준비를 대신하지 않는다.

`verify.py`의 통과는 K8s 접근과 파일 쓰기, 인증된 앱 차단 상태 조회에 한정된다.
ConfigMap 데이터는 바꾸지 않으며 patch 권한은 SelfSubjectAccessReview로만 확인한다.
원격 pooler/DB 연결, RELOAD, 목적지 영속화, 파드 재생성·권한 갱신, 전체 컷오버는 별도 검증이다.
앱이 이미 쓰기를 허용하면 시험은 실패하며, 임의로 차단/해제하지 않는다.
원시 provider 출력과 토큰은 결과에 포함하지 않는다.

```sh
python3 -m unittest discover -s scripts/tests -p test_cutover_access.py -v
```
