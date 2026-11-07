# GCP network module

`network/gcp`(A10, ADR-0034): GKE용 custom VPC 하나, 리전 서브넷 하나(파드 · 서비스 보조 범위, VPC-native), Cloud Router + Cloud NAT(사설 노드의 외부 통신), 서브넷 Flow Logs. 리전 기본값은 `asia-northeast3`(서울)이다.

- backend와 자격증명을 선언하지 않는다. 실행은 [`../../../envs/gcp-foundation`](../../../envs/gcp-foundation)에서 한다.
- VPC · 서브넷 · 라우터 · NAT는 GCP에서 label을 받지 않는다. `component` 라벨은 출력 `network.labels`로 클러스터 모듈에 넘겨 라벨을 받는 자원(GKE, 노드, 디스크)에 단다. 이 프로젝트는 데모 전용이라 비용은 프로젝트 단위로도 나뉜다.
- 출력 `network`는 [`cluster/gcp`](../../cluster/gcp)의 입력 계약이다(`infra/stacks/cluster-gke`가 `vars.from`으로 연결).
