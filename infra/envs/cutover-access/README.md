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
