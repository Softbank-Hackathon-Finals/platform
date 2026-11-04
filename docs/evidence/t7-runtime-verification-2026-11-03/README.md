# T7 실제 플랫폼 검증 — 2026-11-03

기존 기반 구축 담당의 배포를 별도 세션에서 읽기 전용으로 검증했다. 이 작업에서는 기존 Terraform/state/cloud-init을 변경하거나 중복 apply하지 않았다. 검증 소스와 공개 결과에는 계정 ID, 실제 인스턴스/VPC/역할 ARN, kubeconfig, 인증서, 토큰을 포함하지 않는다.

- 17:18 KST: 검토 계정 일치, 플랫폼 EC2 1대 running, 공인 IP 없음, IMDSv2 필수, Ubuntu 24.04 SSM Online. [inventory-summary.json](inventory-summary.json).
- 17:22 KST: 고정 읽기 전용 SSM 명령 Success/0. RKE2 v1.35.9+rke2r1 active, API readyz 성공, 노드 1/1 Ready, 시스템 Pod 17개 중 Pending/Failed/Unknown 0, CIS profile 및 kubeconfig 0600 확인. 시험용 SA의 자기 namespace ConfigMap create 허용, Secret 조회 및 다른 namespace 쓰기 거부. [host-readback.json](host-readback.json).
- GHCR HTTPS 401은 레지스트리 도달 확인이며 이미지 pull 성공은 아니다. 실제 사람 Kubernetes 로그인, CIS benchmark 합격도 이 검사로 확인하지 않았다.
- 역할 조회에서 정확한 audience/subject 2개, SSM SendCommand/GetCommandInvocation 권한, attached policy 0을 관찰했다. [oidc-readback.json](oidc-readback.json)의 actual verification false는 이 설정 조회가 실행 증거가 아니라는 뜻이다.
- 이후 실제 [Actions run 41602534226](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41602534226)을 읽었다. builder commit ca52cc7d129e8808fff9e32951d346763b1f1452의 oidc-rke2 job은 생략되지 않고 성공했다. short-lived AWS session, 고정 namespace ConfigMap 배포 및 거부 JSON, 임의 AWS-RunShellScript AccessDenied 표식을 직접 확인했다. [actions-oidc-readback.json](actions-oidc-readback.json). 이 실행은 앱 이미지의 GHCR build/push/pull을 하지 않는다.

로컬 scripts 시험 58개와 Ruff가 통과했다. [당시 시험 출력](https://github.com/Softbank-Hackathon-Finals/platform/blob/6909cda68c92f88fd72e54a63d736662aa76c487/docs/evidence/t7-runtime-verification-2026-11-03/local-tests.txt)과 [검증 소스 해시](sources.json)를 확인할 수 있다. 소스 해시는 최종 검사 코드이며 AWS 실제 probe 해시는 host receipt에서 확인한다. 운영자 인증에 의한 서버 내부 kubectl과 팀원별 Kubernetes 인증은 분리한다. 인스턴스는 운영 기반이므로 시험 종료 후 삭제하지 않았다. 검사 종료로 남은 터널이나 새 키는 없다.

다음은 GHCR 앱 이미지 digest와 실제 pull 증거를 확보해 T6에 전달하고, 개인 인증과 제품 역할별 접근/플랫폼 DB 등 T7의 나머지 항목을 검증하는 것이다. 이 기록만으로 이슈 #12 전체를 완료 처리하지 않는다. [검증 절차](../../t7-runtime-verification.md).

## 리뷰 후 시스템 Pod 준비 검사 강화

초기 schema v1은 시스템 Pod의 phase만 확인해 Running/Ready=False 또는 빈 목록을 준비 성공으로 잘못 받아들일 수 있었다. 원래 [host-readback.json](host-readback.json)과 소스/시험 기록은 이력으로 보존한다. 현재 schema v2는 목록이 비어 있으면 실패하고 Running Pod의 Ready=True를 요구하며, 정상 종료된 Succeeded 작업 Pod는 허용한다. CrashLoopBackOff/Ready=False, 빈 목록, 정상 완료 작업 반례를 검사했다.

17:39:46 KST 같은 운영 서버에서 읽기 전용 SSM 검사를 한 번 더 실행했다. 시스템 Pod 17개 중 `system_pods_not_ready=0`, 노드 1/1 Ready, 나머지 경계 검사도 유지되어 `operator_readiness=PASS`를 확인했다. 최신 판정은 [host-readback-v2.json](host-readback-v2.json)을 사용한다. 전체 시험 [107개](https://github.com/Softbank-Hackathon-Finals/platform/blob/6909cda68c92f88fd72e54a63d736662aa76c487/docs/evidence/t7-runtime-verification-2026-11-03/local-tests-v2.txt)와 Ruff가 통과했다. 최종 소스 해시는 [sources-v2.json](sources-v2.json)에 있다.
