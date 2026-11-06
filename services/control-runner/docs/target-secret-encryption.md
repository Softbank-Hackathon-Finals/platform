# 목적지 ENC-02.1 — RKE2와 OpenBao 저장 암호화 상태

`TargetSecretEncryptionProbe`는 T5가 지정한 RKE2 secrets-encryption 상태와
OpenBao seal 상태를 읽는다. 암호화 구현·설정 변경·회전은 기존 제품과 담당자의 책임이다.
이 코드는 활성화 메타데이터를 비교하며 저장소의 모든 과거 데이터가 암호화됐다는 증명은 아니다.

## 운영 호스트 인수

- 실제 목적지 클러스터와 **전체 제어 노드 UID 집합**, OpenBao 클러스터 UUID·실행 버전·
  workload/영속 volume UID, 앱 Secret UID·전달 revision을 서버가 등록한다.
- `read_registration`은 매번 실제 인벤토리와 결속을 확인해야 한다. 고정 JSON을 돌려주는
  함수나 업로드된 값은 운영 등록 검증을 대신하지 않는다. OpenBao가 dev/in-memory 실행이
  아니며 등록된 volume에 저장하는지, PebbleDB의 `:memory:` 경로가 아닌지도 호스트가 확인한다.
- 각 노드의 `LocalRKE2StatusReader`를 인증된 호스트 실행 경로로 호출한다. 원격 문자열의
  node UID를 신뢰하지 않는다. 지정한 바이너리 SHA-256과 버전을 읽고 고정 명령만 실행한다.
- OpenBao는 등록된 HTTPS origin과 명시적 CA로 `/v1/sys/seal-status` 하나만 조회한다.
  이 API에는 토큰이 필요하지 않으므로 관리자 토큰이나 앱 비밀을 넘기지 않는다.
- 호스트는 전체 60초 실행 제한·최소 권한·영속 증거 저장을 제공한다. 노드의 RKE2 명령은
  로컬 서버 토큰을 내부에서 읽으므로 이 실행 권한을 일반 앱이나 에이전트에 주지 않는다.

첫 지원 버전은 RKE2 `v1.35.9+rke2r1`, OpenBao `2.7.2` 및 같은 버전의 패치 빌드다.
OpenBao Shamir와 file/raft/PebbleDB 상태를 지원한다. 다른 seal·버전은 별도 자격 시험이 필요하다.
RKE2 명령에는 임의 shell·token·server 인자를 전달하지 않으며 부모 환경도 상속하지 않는다.
운영 호스트가 명령 프로세스와 실제 등록 노드의 결속을 책임진다.

## 판정

모든 노드와 OpenBao를 두 번 읽고 등록을 전후 확인한다. 노드 누락·중복 등록, 다른 실행의
결과, 명령 실패·불완전 출력, 설정 불일치·회전 중·시간 초과는 UNKNOWN이다.

| 상태 | 결과 |
|---|---|
| 전체 노드 enable=true, hashmatch=true, 안정 단계·동일 키 상태, 등록된 OpenBao 정상 | enabled |
| 전체 노드가 명시적으로 enable=false이며 나머지 상태 확인 가능 | disabled |
| OpenBao 미초기화·sealed·이전 중·다른 클러스터·비영속 저장 방식 | UNKNOWN |
| 다른 CA/hostname, 리다이렉트, 관측 거부·누락·만료 | UNKNOWN |

`sealed=true`는 저장 암호화가 꺼졌다는 뜻이 아니므로 disabled로 표시하지 않는다.
역시 `sealed=false`만으로 전체 시험을 통과시키지 않는다. RKE2와 현재 자원 결속도 필요하다.
OpenBao 상태 원문, 키 이름, nonce, 서버 오류, 서버 토큰·unseal share는 증빙에 보관하지 않는다.
등록 digest는 무결성 식별자이며 출처 인증이나 서명을 대신하지 않는다.

## 검증과 남은 범위

[실제 OpenBao 시험](../../../docs/evidence/t14-target-seal-2026-11-06/README.md)은
격리된 로컬 OpenBao의 HTTPS·seal·재시작 보존을 확인한다. RKE2 입력은 모의이며 팀 목적지의
두 저장 계층을 검증한 결과가 아니다. 운영 접속 인수 후 전체 노드·앱 전달 경로·저장소를 다시 검사한다.

```sh
cd services/control-runner
uv sync --locked
PYTHONPATH=src uv run --locked python tests/integration/verify_target_seal.py
```

재현 이미지는 ARM64로 고정돼 있다. 호스트가 다르면 에뮬레이션으로 진행하지 말고 같은
업스트림 버전의 네이티브 manifest와 시험 조건을 먼저 검증한다.

## 근거

- [RKE2 저장 암호화](https://docs.rke2.io/security/secrets_encryption): 상태와 회전 절차.
- [RKE2 고정 버전의 명령 구성](https://github.com/rancher/rke2/blob/v1.35.9%2Brke2r1/pkg/cli/cmds/secrets_encrypt.go): 로컬 supervisor 기본 주소와 상태 명령.
- [상태 JSON 구현](https://github.com/k3s-io/k3s/blob/v1.35.9%2Bk3s1/pkg/server/handlers/secrets-encrypt.go): enable·stage·hashmatch·키 이름의 의미.
- [OpenBao seal-status API](https://openbao.org/docs/api/system/seal-status/): 인증 없는 상태 조회와 필드.
- [OpenBao seal 개념](https://openbao.org/docs/concepts/seal/): 잠금과 저장 암호화의 구분.
- [PebbleDB](https://openbao.org/docs/configuration/storage/pebbledb/): 영속 경로와 `:memory:`의 차이. 이 backend 자체에는 HA가 없다.
