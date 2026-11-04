# T14 kube-bench 출력 자격 시험

2026-11-03, 공식 kube-bench **v0.10.2** Darwin arm64 바이너리를 릴리스 체크섬과 대조한 뒤 실행했다. 클러스터 자격 증명은 제공하지 않았다. 이 기록은 **실제 도구 출력과 T14 해석의 검증**이며 EKS·RKE2 보안 적합성 시험이 아니다.

| 설정 | 선택한 항목 | 실제 결과 | IAM-01.2 채택 |
|---|---|---|---|
| `rke2-cis-1.7` | `5.1.1`–`5.1.6` | 6개 모두 `type=manual`, `status=WARN`, 종료 코드 0 | 미채택 |
| `eks-1.5.0` | `4.1.1`–`4.1.6` | 6개 모두 `type=manual`, `status=WARN`, 종료 코드 0 | 미채택 |

EKS 설정의 일부 설명에는 `Automated`가 있지만 실제 `type`은 `manual`이다. 제목, 종료 코드 0, 실패 건수 0만으로 권한 검증 성공을 판단하면 안 된다. T14는 후보 매핑으로 이 출력을 읽어도 UNKNOWN(`UNEXPECTED_RESPONSE`)을 반환하고, 미확정 매핑은 기존 계약대로 UNKNOWN(`NOT_RUN`)으로 남긴다.

## 재현

1. [공식 릴리스](https://github.com/aquasecurity/kube-bench/releases/tag/v0.10.2)의 바이너리와 `kube-bench_0.10.2_checksums.txt`를 받아 SHA-256을 확인한다.
2. 동일 태그의 `cfg/` 전체를 준비한다. 실행 파일과 설정은 신뢰하는 운영자가 제공하며 사용자 ZIP에서 가져오지 않는다.
3. 자격 증명이 없는 별도 실행 환경에서 아래 명령을 실행한다. `TOOL_DIR`은 검증한 바이너리·설정 디렉터리다. AWS 결과 전송·DB 결과 저장 옵션은 사용하지 않는다.

```sh
KUBECONFIG=/dev/null "$TOOL_DIR/kube-bench" run \
  --config-dir "$TOOL_DIR/cfg" --config "$TOOL_DIR/cfg/config.yaml" \
  --benchmark rke2-cis-1.7 --targets policies \
  --check 5.1.1,5.1.2,5.1.3,5.1.4,5.1.5,5.1.6 --json

KUBECONFIG=/dev/null "$TOOL_DIR/kube-bench" run \
  --config-dir "$TOOL_DIR/cfg" --config "$TOOL_DIR/cfg/config.yaml" \
  --benchmark eks-1.5.0 --targets policies \
  --check 4.1.1,4.1.2,4.1.3,4.1.4,4.1.5,4.1.6 --json
```

이번 실행에서는 서버 버전을 자동 탐지하지 못했다는 경고도 발생했다. 따라서 `complete=true`는 시험용 보고서 파싱에만 사용했고, 클러스터 수집 완료나 통제 통과 근거로 발행하지 않았다.

`results.json`에 원본 출력·바이너리·설정 해시를 기록했다. 회귀 시험의 JSON은 실제 출력에서 `version`, `test_number`, `status`, `type`, `reason`만 추출한 **축약본**이다. 원본과 바이트가 같다고 주장하지 않으며 원본 출력은 비공개로 보존한다.

## 다음 자격 조건

- T6·T30 실제 Kubernetes 배포판·버전에 맞는 벤치마크를 선정하고 고정한다. 이번 오프라인 시험이 그 버전 호환성을 입증하지 않는다.
- 앱 ServiceAccount, namespace, Role/ClusterRole, RoleBinding/ClusterRoleBinding의 UID·기준선·수집 시점을 결속해야 한다. 클러스터 전체 요약을 앱 권한 검사로 대체하지 않는다.
- 수동 항목에는 승인된 별도 읽기 전용 권한 분석과 실제 접근 시험을 연결해야 한다. 시험 방법 변경은 T5/T19 계약 검토를 거친다.
- 정상 앱, 의도한 과권한, 권한 회수, 수집 권한 부족, 누락·오래된 증거에 대한 결과를 실제 대상에서 검증한 뒤 매핑을 확정한다.
- Prowler의 AWS IAM 매핑은 별도 자격 대상이다. 이번 PR은 그 결과를 대신하지 않는다.

현재 IAM-01.2 매핑은 **미확정**이다. 보안 검사를 생략하거나 필수 통제를 PASS로 바꾸지 않는다.

근거: [RKE2 설정](https://github.com/aquasecurity/kube-bench/blob/v0.10.2/cfg/rke2-cis-1.7/policies.yaml), [EKS 설정](https://github.com/aquasecurity/kube-bench/blob/v0.10.2/cfg/eks-1.5.0/policies.yaml), [일반 CIS 설정](https://github.com/aquasecurity/kube-bench/blob/v0.10.2/cfg/cis-1.10/policies.yaml). 상위 프로젝트는 Apache-2.0 라이선스이며 여기에는 전체 설정·보완 절차를 복제하지 않았다.
