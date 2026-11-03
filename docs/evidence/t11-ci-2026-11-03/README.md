# T11 워커의 AWS 실행·컨테이너 검증

2026-11-03 Seoul, 임시 AWS 검증 호스트에서 `services/agent-worker`의 commit `7dcbe6c565b292c81f330a7cd2cf7faed8d441e7`을 검증했다. **실제 OpenAI 모델을 호출하지 않았다.** 유지관리되는 코드·가짜 provider/API 시험·합성 CLI 입력을 실행한 결과다. Bedrock 연결과 개인 OAuth 복사는 하지 않았다.

## 검증 결과

| 항목 | 실제 결과 |
|---|---|
| `uv sync --locked` | 잠긴 의존성 설치 성공 |
| `uv run --locked ruff check .` | 통과 |
| `uv run --locked pytest -q` | **74 passed, 4.35초** |
| `uv build --wheel` | `bravo_agent_worker-0.1.0-py3-none-any.whl` 생성 |
| Dockerfile 빌드 | Linux arm64 이미지 생성 |
| 제한된 컨테이너 CLI·경계 시험 | **5개 시나리오 모두 기대 결과** |

컨테이너 이미지는 로컬 Docker content ID **`sha256:5bcca642dca65aecbe3816116cfbcb337e1dd7f65caa61a16280176639c4e49a`**다. 레지스트리에 push하지 않았으므로 배포 가능한 registry digest와 구분한다. wheel SHA-256과 실제 base image 해석 결과는 [AWS 영수증](aws-runtime-results.json)에 있다.

## 실제 컨테이너에서 확인한 5개 시나리오

1. **스키마 출력:** 네트워크 없이 JSON Schema를 출력했다. 추가 필드 거부와 최대 수정 파일 4개를 확인했다.
2. **실행 경계:** UID/GID 10001, 읽기 전용 root filesystem, 입력 `:ro`, network none, 전체 capability 제거, no-new-privileges, PID 64·메모리 512 MiB·CPU 1 한도를 실제 `docker inspect`로 읽었다. Python probe에서도 root/input 쓰기가 거부되고 외부 TCP 연결이 실패했다. effective capability 0, NoNewPrivs 1, Docker socket·Kubernetes token·모델/클라우드 credential 환경변수 없음도 확인했다. 기본 이미지에는 선택 기능인 Claude SDK가 없다.
3. **모델 키 없음:** 합성 입력으로 `analyze`를 실행하면 종료 코드 2, `NEEDS_HUMAN`, proposal 없음, `MODEL_OR_POLICY_FAILURE` 감사 기록이 생겼다. 실제 API 호출 성공으로 해석하지 않는다.
4. **기존 출력 보호:** 같은 출력 경로로 다시 요청하면 종료 코드 2이며 기존 결과 파일의 SHA-256은 바뀌지 않았다.
5. **잘못된 입력:** `{}` 입력은 종료 코드 2로 거부되고 결과 파일을 만들지 않았다.

컨테이너는 제한된 `/tmp` tmpfs와 별도 결과 디렉터리만 쓸 수 있게 실행했다. 이는 **제안 워커 CLI의 실행 경계**다. 에이전트가 만든 임의 코드를 T15 리허설에서 실행하거나 비공개 검증 Job으로 판정한 증거는 아니다. 이 검증의 network none은 오프라인 시험 조건이며, 실제 모델 endpoint 허용 정책의 런타임 검증을 대신하지 않는다.

## 재현과 출처

[소스 manifest](source-manifest.json)는 해당 commit에서 추출한 22개 파일의 경로와 SHA-256, archive SHA-256, SSM 명령 ID를 담는다. `.git`, `.env`, Terraform state/plan, 운영자 홈·자격증명은 전송하지 않았다. 호스트는 T7 임시 검증 root의 `i-05c13c836cf78b0a2`이며 root의 종료 타이머·Terraform 정리 범위 안에서 사용했다. 해당 호스트의 SSM instance role은 Parameter 값·Secrets Manager 값·KMS 복호화를 명시적으로 거부한다.

시험 후 비교에서 실행 코드·테스트·lock·Dockerfile은 전송 시점과 일치했다. 로컬 `services/agent-worker/README.md`의 후속 설명 변경만 달랐다. 이미지와 wheel은 위 commit의 README를 포함한다.

로그:

- [74개 시험](t11-tests.log)
- [Ruff](t11-ruff.log)
- [wheel 빌드](t11-build-wheel.log)
- [AWS 실행 정책·5개 결과·이미지·wheel 영수증](aws-runtime-results.json)

검증 과정에서 Ubuntu 지역 ARM 미러를 HTTP에서 HTTPS로 바꾸기만 하면 인증서 hostname이 맞지 않는 사례를 확인했다. 공식 `https://ports.ubuntu.com/ubuntu-ports`로 정규화한 뒤 Docker를 설치했고 TLS 검증은 유지했다. Ubuntu는 Buildx를 별도 패키지로 제공해 `docker-buildx`도 설치했다. 이 호스트 준비 수정은 워커 소스나 판정 기준 변경이 아니다. T7 cloud-init에 같은 URI 정규화를 반영하는 변경은 별도로 리뷰한다.

실제 모델 응답, T10 영속 상태·승인, T15 실행·비공개 검증, T31 반영은 이 증거 범위 밖이며 T11 전체 완료 체크의 대체물이 아니다.

검증 뒤 15:55 KST에 임시 root의 16개 자원을 삭제했다. state 잔여 0, 인스턴스 terminated와 VPC·루트 EBS·IAM 역할 부재를 [AWS 재조회](cleanup-readback.json)로 확인했다.
