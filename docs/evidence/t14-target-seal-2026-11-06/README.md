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

RKE2 실제 CLI 실행·전체 제어 노드·실제 Secret 전달 결속·운영 영속 저장은 미검증이다.
이 시험의 모의 RKE2 입력을 운영 원문으로 제출하지 않는다.

[호스트 인수 계약과 재현](../../../services/control-runner/docs/target-secret-encryption.md)
