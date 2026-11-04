# T7 운영 환경 접속 검증

이 절차는 이미 구축된 플랫폼 EC2를 검증한다. Terraform/state/cloud-init 변경은 기존 기반 구축 담당이 소유하고, 이 도구는 고정된 읽기 전용 호스트 검사만 실행한다. 현재 사람이 사용하는 IAM AdministratorAccess와 Actions/제품 역할의 권한은 별도다.

## 검증한 경로

운영자의 기존 AWS 인증 → 검토된 계정과 인스턴스 경계 확인 → SSM Run Command → 서버 내부 Kubernetes API 순서로 검사한다. 서버의 kubeconfig를 읽어 출력하거나 밖으로 복사하지 않는다. 이 경로가 성공한 사실은 팀원별 Kubernetes 인증이나 SSO 로그인을 검증한 사실과 다르다.

로컬 입력은 저장소 밖 mode 0600 파일에 둔다. 아래 값은 예시이며, 실제 값은 기존 기반 state 출력과 EC2 조회를 대조해 운영자가 채운다. 현재 조회 결과를 아무 확인 없이 expected 값으로 복사하지 않는다.

```json
{
  "expected_account": "111122223333",
  "region": "ap-northeast-2",
  "instance_id": "i-example",
  "vpc_id": "vpc-example"
}
```

```sh
python3 scripts/t7_runtime_readback.py \
  --config /private/path/t7-runtime.json \
  --private-receipt /private/path/ssm-receipt-unique.json
```

실행 전 계정·인스턴스·기반 VPC·`project=bravo-platform`·`component=platform`·Terraform 소유 태그·전용 인스턴스 프로필·공인 IP 없음·running을 확인한다. 다른 자원이나 상태 불일치가 있으면 명령을 보내지 않는다. SSM 명령 ID와 실제 대상 ID는 비공개 receipt에만 보관한다. 같은 receipt 파일을 덮어쓰지 않는다.

SSM에서 하는 일은 서비스 상태, `/readyz`, 노드 Ready, 시스템 Pod 수/Ready 조건/비정상 phase 수, CIS 프로필 설정, kubeconfig 파일 권한, 제한된 smoke 서비스 계정의 RBAC 판정, GHCR HTTPS 도달 조회다. `kube-system` Pod 목록이 비어 있으면 실패하며, Running Pod는 Ready=True여야 한다. 정상 종료된 Succeeded 작업 Pod는 허용한다. 임의 명령·매니페스트 입력이나 쓰기 동작을 받지 않는다. 표준출력은 정해진 boolean·숫자·버전·열거값만 허용하고 원격 stdout/stderr·오류 원문은 내보내지 않는다. 로컬에 AWS 자격증명이나 kubeconfig를 새로 만들지 않는다.

| 결과 | 뜻 |
|---|---|
| exit 0, `operator_readiness=PASS` | SSM 명령 성공과 정해진 RKE2/RBAC 준비 항목이 모두 확인됨 |
| exit 2, `operator_readiness=NOT_READY` | 명령은 성공했으나 서비스·노드·권한 등 준비 조건이 충족되지 않음 |
| exit 1, `readiness=NOT_VERIFIED` | 계정/소유 경계, API 응답, 실행 또는 결과 스키마를 신뢰할 수 없음 |

프로세스 실행 성공만으로 준비 완료를 표시하지 않는다. 이 검사는 `kube-system` Pod의 Ready 조건 또는 정상 종료를 확인하며, 다른 namespace의 앱 workload까지 검증하지 않는다. `profile: cis`는 설정 확인이며 CIS benchmark 합격 판정이 아니다. `ghcr_http_status=401`은 레지스트리에 도달했다는 뜻이고 이미지 인증·pull 성공이 아니다.

## Actions OIDC와 배포 증거

현재 기반 작업의 `runtime-smoke`는 정확한 repository/branch subject와 `sts.amazonaws.com` audience를 신뢰하는 역할을 사용한다. 고정 SSM 문서와 지정 EC2에 대한 실행만 허용하고 임의 `AWS-RunShellScript` 문서는 거부한다. 이 검증 도구를 실행하는 운영자 IAM 권한과 같은 수준으로 확장하지 않는다.

Actions 성공을 판단할 때 전체 run conclusion만 보지 않고 실제 job/인증 단계 실행 여부, 고정 namespace 배포 결과, Secret 및 다른 namespace 거부, 임의 shell 문서의 AccessDenied를 확인한다. 생략된 job이나 로컬 IAM 호출을 OIDC 성공으로 기록하지 않는다. workflow/신뢰 정책/배포 실행을 바꾸기 전 기반 담당과 범위를 조정한다.

GHCR는 별도 증거가 필요하다. 실제 앱 build/push의 digest, 그 digest의 registry 존재, 소비 클러스터의 pull 성공을 같은 이미지에 대해 확인한다. 고정 ConfigMap 배포만으로 이미지 배포 성공을 주장하지 않는다. 검토 중인 T7 기반에 GHCR workflow가 없는 시점의 확인 결과는 [실측 기록](evidence/t7-runtime-verification-2026-11-03/README.md)에 구별했다.

## 팀원 접속 및 출발지 EKS

현재 서버 내부 kubeconfig 경로는 RKE2가 제공하는 관리자 접속 경로다. 실제 사용자별 인증 절차 없이 이를 팀원에게 배포하지 않는다. 개인 AWS 인증으로 SSM을 통과하는 경로와 Kubernetes 개인 권한은 각각 확인하고, 장기 키·토큰·인증서를 공개 자료에 넣지 않는다. [RKE2 접속 문서](https://docs.rke2.io/cluster_access).

출발지 EKS private API의 관리 경로는 이 서버를 SSM remote-host 포트 포워딩 중계로 사용할 수 있다. 해당 경로의 검증과 workload 적용은 T6 담당이 수행한다. 로컬 AWS exec 인증 토큰과 실제 EKS TLS hostname을 유지하고, 노드에 AWS 키를 복사하거나 노드 역할의 Secret/KMS deny를 해제하지 않는다. 임시 터널은 이를 시작한 세션이 소유하고 종료하며, 운영 EC2와 NAT/state는 시험 자원처럼 삭제하지 않는다. 이 설명만으로 EKS 접속 성공을 표시하지 않는다.

## 버전과 로컬 검사

2026-11-03 공식 RKE2 1.35 release notes에서 기반이 고정한 `v1.35.9+rke2r1`을 확인했다. CIS 가이드는 1.35 이상에 CIS v2.0 self-assessment를 연결한다. 최신 제품 전체 버전이라는 뜻이나 benchmark 실행 결과로 사용하지 않는다. [RKE2 1.35 릴리스](https://docs.rke2.io/release-notes/v1.35.X), [CIS 가이드](https://docs.rke2.io/security/hardening_guide), [GitHub OIDC 조건](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).

```sh
python3 -m unittest discover -s scripts/tests -p test_t7_runtime_readback.py -v
uvx ruff check --target-version py39 scripts/t7_runtime_checks.py scripts/t7_runtime_readback.py scripts/tests/test_t7_runtime_readback.py
```
