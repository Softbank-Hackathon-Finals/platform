# 제품 입력 조회 이미지

이 이미지는 [인증 조회 서비스](../approval/PRODUCT-READ.md) 전용이다. 기존 window
수신 이미지와 따로 빌드한다. 클러스터 배포나 registry 게시를 자동 실행하지 않는다.

병합된 정확한 `develop` checkout에서 다음을 실행한다. 출력 경로는 아직 없어야
하며 저장소 밖의 비공개 작업 폴더를 사용한다.

```sh
uv run --project services/platform-events python \
  services/platform-events/deploy/product-read/build.py --output "$BUILD_OUTPUT"
```

빌더는 변경 없는 커밋과 잠긴 `product-inputs`·`postgres` 의존성을 사용한다. Git의
일반 blob으로 등록된 다섯 Python 패키지만 복사하고, symlink·서브모듈을 거부한다.
무시된 설정·로컬 수정·키·DB 자격·전체 checkout은 build context에 넣지 않는다.
observability는 기존 고정 revision을 유지한다. registry 패키지는 버전과 해시를
검사하고, Python base도 기존 검토 이미지 digest에 고정한다.

빌드 대상은 `linux/amd64`, 실행 사용자는 `10001:10001`, 포트는 TLS 8443이다.
빌드 후 네트워크·쓰기·추가 capability 없이 실제 컨테이너에서 조회 모듈을
import한다. 최초 제안 모드가 지연 로드하는 API와 저장 원본 조회 모듈도 포함하며,
모델 runtime·운영자 dispatch·mailbox·최초 제안 transport와 그 하위 모듈이
로드되지 않았는지 검사한다. 조회 API와 운영자 transport의 workflow 이름은
의존성이 없는 `initial_proposal_contracts`에서 공유한다.

출력은 `image.tar`와 `build.json`이다. 영수증은 소스 revision, 각 build 파일 해시,
context 해시, 로컬 image ID, archive 해시와 import 결과를 담는다. OCI archive의
index·amd64 manifest·config를 해시와 크기로 다시 대조하고 platform manifest digest와
UID/GID를 따로 기록한다. OCI `Descriptor`와 archive를 지원하는 Docker가 필요하다.
로컬 image ID만 registry manifest digest라고 간주하지 않는다. 수신자는 전달된
archive와 manifest를 대조한 뒤 기존 인프라 담당의 절차로 import한다.

설정·공개 JWKS·catalog·현재 grant·TLS·DB mount는 이미지를 실행할 때 별도로
인수한다. 빌드나 import 검사는 실제 HTTPS·운영 등록·현재 권한·모델 실행을
증명하지 않는다. 실행 입력이 없으면 서비스를 시작하지 않는다.

최초 PROPOSE 입력을 조회할 때는 같은 실행 명령에 `--initial-proposal`을 추가한다.
이 모드는 `/internal/v1/initial-proposal-inputs/read`만 열고, 신뢰된 호스트가 이미
접수한 불변 입력을 기존 DB에서 읽는다. 원본 checkout·수집 자격을 조회 Pod에
복사하지 않는다. 현재 계획·catalog·별도 `t38-initial-proposal` grant·저장된
실행 claim과 GitHub OIDC의 workflow/source/run/attempt가 일치해야 한다.
입력이 없으면 거부하며 새 입력·grant·모델 실행을 만들지 않는다.
