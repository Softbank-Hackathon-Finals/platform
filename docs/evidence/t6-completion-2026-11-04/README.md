# T6 예선 원본 검토와 완료 기준 확인

T6의 목표와 완료 기준은 유지한다. 팀 앱이 실제 EKS에 배포되고 PgBouncer를 경유해 DB에 연결되어야 하며, pgAudit·CloudTrail이 활성화되어야 한다. 기존 할 일의 “예선 EKS 모듈”은 아래 원본 조사 결과와 맞지 않아, **예선 인프라의 적용 가능성 검토와 현재 EKS 모듈 구조·실환경 검증 근거 확인**으로 구체화한다. 체크박스는 이 문서에서 완료 처리하지 않는다. 검토·병합 뒤 [T6 이슈 #11](https://github.com/Softbank-Hackathon-Finals/platform/issues/11)에서 판단한다.

## 예선 원본을 확인한 범위

2026-11-04 원격 heads/tags를 조회한 결과 Railshot 35개, Jasmin 8개 브랜치와 태그 0개였다. 모든 head 객체를 확보한 비 shallow 저장소에서 각 tip과 해당 head들에 도달 가능한 이력을 검사했다. Terraform·YAML·shell·Python·TypeScript·Go 구현에서 `aws_eks_`, `eksctl`, `terraform-aws-modules/eks`를 검색했으며 구현 선언은 발견되지 않았다. tip 문서의 EKS 언급은 아래 설계의 명시적 비적용 설명이었다. 삭제된 원격 ref, 접근 불가 저장소, 미게시 개인 작업까지 없다고 주장하지 않는다.

| 원본 | 확인한 구조 | T6 적용 판단 |
|---|---|---|
| [Railshot 설계, `645e28ae`](https://github.com/Jasmin-Softbank/Railshot/blob/645e28aefa7f3d3996c57ea6853c6e2f67c5bcc4/docs/architecture/self-managed-ingress.md) | VM 직접 설치 K3s + Cilium. EKS/GKE/Magnum을 전제로 하지 않음 | EKS 재사용 원본으로 취급할 수 없음 |
| [Railshot AWS Terraform, 같은 commit](https://github.com/Jasmin-Softbank/Railshot/blob/645e28aefa7f3d3996c57ea6853c6e2f67c5bcc4/infrastructure/terraform/aws/main.tf) | EC2·SSM 역할·보안 그룹·암호화 디스크·선택적 CI OIDC | 수명·역할 분리의 비교 자료. 그대로 본선 EKS에 적용하지 않음 |
| [Jasmin, `e1003735`](https://github.com/Jasmin-Softbank/Jasmin/tree/e100373528352f1b9594311c643e9ad30ba87580) 및 나머지 7개 head | 지정 구현 검색과 도달 가능한 이력에서 EKS 선언 미발견 | 확인한 범위의 미발견으로 기록 |

[검토 결과 JSON](results.json)에 당시 43개 ref/SHA, 검색 범위와 원본 영수증 해시를 남겼다. 예선 모듈을 찾았다는 체크로 바꾸지 않고 현재 구현을 직접 검토한다.

## 현재 팀 구현과의 대조

검토 기준은 `platform`의 `d3b00c8c59d7d6a3a6ed0fff4c4419d1a046bb13`이다. 개인 구현이나 예선 실측을 현재 배포 증거로 사용하지 않는다.

| 경계 | 현재 구현 | 확인 근거 |
|---|---|---|
| 기반 수명 | T1 foundation 출력의 출발지 VPC·서브넷을 소비하고 별도 T6 state 소유 | [source-eks](../../../infra/envs/source-eks/README.md), `main.tf` |
| 클러스터·접근 | private EKS, 명시적 access entry, 전용 CNI IRSA, 암호화·5종 control-plane 로그 | 같은 모듈의 [경계 시험](../../../infra/envs/source-eks/tests/boundaries.tftest.hcl), [11/3 실제 ACTIVE·Ready·허용/거부·감사 관측](../t6-runtime-2026-11-03/README.md) |
| 워크로드 | 별도 state의 CNPG·PgBouncer·ESO, 앱/복제/제어 역할 분리, S3·Secrets Manager·KMS | [source-workloads](../../../infra/envs/source-workloads/README.md), 11/3 구축·업무 HTTP 31개 검증 |
| 현재 앱 DB 경로 | 현재 앱의 `DB_HOST`가 PgBouncer, `verify-full`, `/readyz`와 `/api/health/db` 200 | 11/4 11:36 KST 읽기 관측 |
| 감사·시크릿 | pgAudit 확장 및 `ddl,role`, parameter logging off; CloudTrail logging true/전달 오류 없음; ESO Ready | 11/4 11:34 KST 읽기 관측. 실제 이벤트 전달은 11/3 기록을 별도 인용 |
| 소비 권한 | 최종 `2364a76` 검증기로 출발지 27/27, 목적지 pooler-only 26/26 통과 | [#114 승인·병합](https://github.com/Softbank-Hackathon-Finals/platform/pull/114), 11/4 11:32~11:33 KST 실행. 현재 Pod/SA와 설정 보존·임시 파일 부재 확인 |

11/4 실행은 기존 환경 소유 세션이 수행했고 이 문서 작성 세션은 원본 영수증, 검증기 해시와 현재 소스를 대조했다. 중복 실환경 시험은 수행하지 않았다. 처음의 잘못된 `PGHOST` 변수·health 경로 조회와 원인 미확정 운영자 호출 실패 1회는 원본에 보존했다. 실제 `DB_HOST` 및 `/api/health/db`로 명시적 상태를 확인한 후속 성공과 구별하며 제품 설정은 바꾸지 않았다.

### 병합 후 Secret 읽기 검증 지적

위 27+26개 결과의 Secret 검사는 **목록 조회 거부**이며 특정 이름의 `get`이나 모든 Secret 읽기 차단을 증명하지 않는다. [#114 후속 지적](https://github.com/Softbank-Hackathon-Finals/platform/pull/114#issuecomment-6620831141)은 이름별 `get`만 허용된 Role을 합치면 기존 검증이 잘못 통과할 수 있음을 지적했다. [#178](https://github.com/Softbank-Hackathon-Finals/platform/pull/178)이 완전한 권한 목록과 이름별 허용 여부를 확인하도록 보완하며, 불완전한 응답은 실패·운영자 확인 필요로 남긴다.

후속 출발지 관측에서는 자동 권한 목록이 불완전해 전체 Secret 읽기 거부를 PASS로 올리지 않는다. 운영자가 별도로 합산 RBAC와 현재 Pod가 참조하는 Secret 이름을 대조한 결과는 그 범위의 근거다. 다른 namespace나 임의의 외부 authorizer까지 확인했다고 확대하지 않는다. 기존 JSON의 성공 값은 당시 소스와 범위를 유지하며 덮어쓰지 않는다. T6 종료 시에는 #178의 최종 검증·리뷰 결과와 이 한계를 함께 확인한다.

## 이슈에서 판단할 항목

1. 예선 적용 가능성 및 현재 모듈 검토: 위 원본·검색 범위·구조 비교로 확인한다. 존재를 확인하지 못한 EKS 모듈을 재사용한 것으로 표시하지 않는다.
2. 앱의 PgBouncer 경유 DB 접속: 11/3 실제 업무 경로와 11/4 현재 앱 DB health 관측으로 확인한다. 통합 시점 선행인 [T4 #8](https://github.com/Softbank-Hackathon-Finals/platform/issues/8)은 완료 상태를 별도로 확인했다.
3. pgAudit·CloudTrail 활성화: 11/3 실제 감사 이벤트 전달과 11/4 현재 활성·전달 오류 없음 관측을 구분해 확인한다.

T1 개인 SSO 인수, T8 전체 왕복 복제, T12 비교기 연결·배포·실제 컷오버/실패 분기, T14 전체 통제, T34 수집 통합은 각각의 완료 기준을 따른다. 이번 출발지 준비 확인은 이들의 완료나 전체 제품 준비를 뜻하지 않는다. 목적지 앱 접근은 `false`, 양쪽 `cutover_ready`는 `false`다.

새 VM·DB·클라우드 자원을 만들지 않았다. 실행 영수증에서 시험 파일 부재와 기존 Pod/SA/Role/Binding/ConfigMap/INI 보존을 확인했고, 현재 운영 출발지와 기존 T8 VM은 유지한다. 원본 식별자와 접속 정보는 비공개로 보존한다. 공개 JSON은 선택 필드 요약이며 원본 전체나 독립적인 공급자 증명으로 표시하지 않는다.
