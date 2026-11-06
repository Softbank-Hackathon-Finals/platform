# T14 목적지 저장 암호화 — OpenBao 실제 부분 시험

2026-11-06, 공식 OpenBao 2.7.2 ARM64와 PebbleDB로 소유한 로컬 컨테이너에서 검증했다.
RKE2 상태·등록 앱·노드 인벤토리는 모의 입력이다. 팀 온프렘 목적지 인수 또는 양쪽 PASS가 아니다.

| 실제 시험 | 결과 |
|---|---|
| 초기화 전 상태 조회 | initialized=false 확인 |
| 초기화·unseal 후 인증서를 검증하는 HTTPS 조회 | 성공 |
| 실제 OpenBao + 모의 정상 RKE2 상태로 수집기 실행 | enabled; 부분 자격만 인정 |
| 다른 클러스터 UUID로 등록 | UNKNOWN |
| 잘못된 CA / hostname | 각각 UNKNOWN |
| 자체 시험 OpenBao seal | UNKNOWN; 평문 저장 실패로 오인하지 않음 |
| 자체 시험 unseal | 정상 상태 재조회 |
| 컨테이너 재시작 | 초기화 보존·sealed=true·unseal 후 동일 클러스터 확인 |
| 시험 자원 정리 | 자체 label 확인 후 컨테이너·임시 파일 삭제 완료 |

이미지: `quay.io/openbao/openbao@sha256:dcf8c71424760a76368966b07227fff45ff49944fbbd53994afb6b83d205b08b`.
팀의 별도 보안 패치 이미지와 같은 것으로 주장하지 않는다. 실제 native ARM64인지 검사했다.
UID100/GID1000, capability 제거, no-new-privileges, CPU/메모리/PID 제한을 적용했다.
포트는 `127.0.0.1`에만 공개했다. 일회용 root token·unseal share는 시험 프로세스 메모리에서만
사용했으며 결과·파일·명령 인자에 기록하지 않았다. 제품 수집기에는 토큰이 전달되지 않는다.

실패 기록: 최초 실행은 비 root 데이터 디렉터리의 쓰기 권한 때문에 시작하지 못했다.
자체 writable 경로로 수정했다. 두 번째 실행은 재시작 뒤 임시 공개 포트 변경을 반영하지 못했다.
실제 loopback binding을 다시 읽은 뒤 전체 시험을 통과했다. 어느 실행에서도 기존 팀 자원을
사용하거나 운영 보안 설정을 완화하지 않았다. 실패한 실행의 자체 컨테이너도 정리했다.

## 11:42 KST — 실제 플랫폼 RKE2 명령 자격

별도로 등록된 **AWS 플랫폼 호스트**에서 SSM의 고정 읽기 명령을 실행했다.
개인 SSO 신원·등록 인스턴스 태그·kube-system UID를 확인하고 제어 노드 1개의 UID/버전을 읽었다.
RKE2 `v1.35.9+rke2r1`(`8229013e2b1a73eef1b41dd93708fa96cf11f11f`)에서
`secrets-encrypt status --output json`을 두 번 실행해 동일 상태를 확인했다.
enable=true, hashmatch=true, stage=start와 AES-CBC provider를 실제 제품 파서가 수용했다.
수집 시각은 2026-11-06T02:42:45Z이며 원문·SSM 실행 ID·바이너리 해시는 비공개로 보존했다.

설정 파일·Secret 값은 읽지 않았고 회전·재시작·권한·자원을 변경하지 않았다. 별도 클러스터나
8GB VM을 추가하는 대신 기존 등록 호스트의 읽기만 사용했다. 이 명령은 서버 토큰을 내부에서
읽기 때문에 향후 운영 수집 주체에는 고정 명령 권한만 전달해야 한다.

**이 플랫폼 호스트는 온프렘 목적지가 아니다.** 실제 CLI 출력 파서는 검증했지만 목적지의
전체 제어 노드·Secret 전달·volume/runtime 결속은 미검증이다. 위 로컬 OpenBao와 이 플랫폼
RKE2의 결과를 합쳐 한 목적지의 ENC-02.1 PASS로 만들지 않는다. LocalRKE2StatusReader 자체의
호스트 설치·인증된 원격 실행 연결도 별도 인수다.

[호스트 인수 계약과 재현](../../../services/control-runner/docs/target-secret-encryption.md)
