# ADR-0012: 단일 노드 플랫폼 DB와 수집·제어 접근 경계

* 상태: 제안됨 — 사용자 승인 범위의 개발 환경에 적용·검증, 팀 채택은 이 PR 리뷰 후
* 날짜: 2026-11-03
* 관련: T7 #12, T10 #19, ADR-0009, 설계 7.5·8.2·8.6

## 배경과 결정

T7의 기존 EC2/RKE2는 OIDC와 이미지 다운로드를 검증했지만 플랫폼 상태 DB가 없었다. T10은 같은 트랜잭션의 상태·이벤트·명령 영수증을 보존하는 PostgreSQL 어댑터를 제공한다. 기존 출발지 채팅 DB를 플랫폼 저장소로 공유하지 않고, **기존 RKE2에 별도 CNPG PostgreSQL 18.4 단일 인스턴스**를 둔다. T6와 같은 CNPG 1.30.1·고정 이미지 digest를 재사용한다.

새 RDS나 EC2는 추가하지 않는다. 별도 암호화 gp3 16GiB를 Terraform의 독립 state가 소유하고, 기존 노드에 연결한 ext4를 정적 local PV로 제공한다. root EBS와 분리하며 EBS/attachment `prevent_destroy`, PV `Retain`, EC2 종료 시 데이터 볼륨 보존을 확인한다. CSI의 클러스터 권한·클라우드 역할을 추가하지 않는 대신, **노드 교체 시 동일 AZ 재연결·mount 검증·PV nodeAffinity 수정은 운영자 작업**이다. 단일 노드/AZ 장애 중 가용성과 off-host 백업을 보장하지 않는다.

`mount_volume.py`는 정확한 EBS serial·크기·기존 mount·파일시스템을 대조한다. 명시한 새 빈 볼륨만 초기화한다. systemd mount와 RKE2의 `RequiresMountsFor`를 연결해 다음 부팅에서 데이터 디스크 없이 root 경로에 DB를 만드는 일을 막는다. 실행 중인 RKE2와 원래 config는 재시작·교체하지 않는다.

## 자격과 실행 경계

| 주체 | 보유 권한 | 부여하지 않는 권한 |
|---|---|---|
| 플랫폼 migration 로그인 | T10 migrator 그룹, 스키마 migration | runtime과 자격 공유, 출발지 DB·Kubernetes 관리 |
| T10 runtime 로그인 | ADR-0009의 테이블 SELECT/INSERT와 제한한 UPDATE | DDL·임시 테이블·tenant/spec 재작성·이벤트/영수증 삭제·역할 승격 |
| collector | 해당 환경의 Pod·Service·CNPG 상태 읽기 | Secret·ConfigMap 원문·쓰기·exec·Pod 생성·토큰 발급 |
| Executor의 초기 Kubernetes 역할 | 같은 조회 + 정확한 PgBouncer ConfigMap 하나 get/patch | 다른 ConfigMap·Secret·exec·Pod 생성·RBAC/토큰 발급 |
| 운영자 | 기존 본인 IAM·SSM으로 검토한 설치와 검증 수행 | 이 권한을 제품 AI 워커·collector·Executor에 전달하지 않음 |

DB 비밀번호는 호스트 메모리에서 생성해 암호화 활성 상태를 확인한 Kubernetes Secret에만 저장한다. 로그·SSM 입력·Terraform state에 비밀번호를 넣지 않는다. migration/runtime Secret을 분리하고 collector/Executor는 각각 별도 namespace에 둔다. runtime 계정도 Kubernetes Secret 조회 권한은 없다. DB 연결은 CNPG CA로 hostname까지 확인하는 TLS와 SCRAM을 사용한다.

출발지의 두 ServiceAccount는 실제 10분 TokenRequest 토큰으로 검증했다. 토큰은 프로세스 메모리에서만 사용했다. 장기 토큰을 플랫폼에 저장하거나 호스트 IAM/CI smoke 역할을 확장하지 않는다. **지속 실행용 단기 토큰 발급·회전과 목적지 적용은 후속 연결 계약**이다.

## 검증과 한계

T10의 합성 계획 등록·COLLECT→ANALYZE·같은 명령 재시도·HTTP snapshot/SSE·다른 tenant 거부를 실제 클라우드 DB에서 검증한다. DB Pod를 재생성한 뒤 별도 Job/새 프로세스가 동일 snapshot·영수증 해시를 읽어야 한다. 이 시험 안의 인증 principal은 합성 fixture이며 외부 API 서버로 노출하지 않는다.

T12의 11/3 후속 요청에는 앱·PgBouncer namespace의 exec와 DB 제어가 포함됐다. 이 초기 T7 역할에는 이를 아직 부여하지 않는다. 별도 T12 소비 계약에서 정확한 namespace·Pod와 파드 내부 자격 사용 경계를 검토한 뒤 opt-in 역할로 연동한다. Kubernetes RBAC는 exec 내부 명령까지 제한하지 못하므로 Secret API 거부를 파드 내부 자격 사용 금지와 혼동하지 않는다. 이번 Executor 역할은 정확한 ConfigMap 변경 권한의 분리이며 **쓰기 차단·PAUSE·펜싱·barrier·구독 전환·RELOAD/RESUME 전체 실행 권한이나 컷오버 성공을 의미하지 않는다**. 출발지 시험은 전용 annotation 하나를 추가·제거하고 실제 PgBouncer 데이터가 그대로임을 확인한다.

T33 저장소 이식, T24 S3 Object Lock, T10 전체 상태 머신·실제 사용자 인증, 목적지와 지속 실행 자격, HA·복원 훈련은 남는다. T7 전체 이슈를 완료 처리하지 않는다.

## 근거

- [CNPG 설치·범위 제한](https://cloudnative-pg.io/docs/1.30/installation_upgrade/), [CNPG 역할 관리](https://cloudnative-pg.io/docs/1.30/declarative_role_management/)
- [Kubernetes local volume의 노드 결속](https://kubernetes.io/docs/concepts/storage/volumes/#local), [RBAC와 권한 확대 경계](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [구현·운영 절차](../../infra/envs/platform-data/README.md), [실측 증거](../evidence/t7-platform-data-2026-11-03/README.md)
