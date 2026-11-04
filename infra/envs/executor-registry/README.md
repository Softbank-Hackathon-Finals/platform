# T7 · Cutover Executor 이미지 게시

Executor는 플랫폼 서비스다. 고객 앱의 `bravo/demo-chat` 저장소와 publisher 역할을
재사용하지 않고, 플랫폼 저장소의 검토된 소스를 `bravo/cutover-executor`에 게시한다.
이 모듈과 워크플로는 T7의 OIDC·이미지 공급 경로이며 T12의 배포·컷오버를 수행하지 않는다.

## 권한과 상태

- 서울 리전, 검증된 개발 계정, 기존 GitHub OIDC provider만 사용한다.
- publisher는 플랫폼 저장소의 불변 조직·저장소 ID와 `develop` ref를 정확히 신뢰한다.
  GitHub의 현재 immutable subject 설정을 확인하고 다른 설정이면 적용을 중단한다.
- 새 ECR 저장소는 불변 태그, KMS 암호화, push scan, 삭제 보호를 사용한다.
- publisher는 이 저장소의 업로드·조회만 허용한다. 기존 플랫폼 호스트 역할에는
  이 저장소의 읽기 정책 하나만 추가한다. `GetAuthorizationToken`의 `Resource: "*"`는
  [AWS의 인증 요구](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html)이며
  이미지 접근은 저장소 ARN으로 제한한다.
- 기존 앱 저장소·publisher·역할 trust는 이 상태의 관리 대상이 아니다.
  기존 팀 S3 backend에 별도 키 `runtime/executor-registry/terraform.tfstate`를 쓴다.
  계정, backend, 역할 ARN, ECR 주소가 담긴 입력·plan·output은 비공개로 보관한다.

적용 전 기존 인증으로 계정을 검증하고, 플랫폼 호스트의 계정·VPC·소유 태그·실행 상태와
역할 연결을 확인한다. Terraform도 `allowed_account_ids`와 역할의 ARN·소유 태그를 검사한다.
새 이름의 저장소나 publisher가 이미 있으면 덮어쓰거나 자동 import하지 않고 소유 상태를 확인한다.
리뷰한 plan은 새 저장소, 새 역할, publisher 정책, 기존 호스트의 별도 읽기 정책만 포함해야 한다.
PR 승인·필수 CI 이후 해당 plan을 적용하고 원격 재조회로 검증한다.

## 게시 순서

저장소 Actions secret에 식별자 세 개를 설정한다. 장기 AWS 키는 필요하지 않다.

| 이름 | Terraform output의 필드 |
|---|---|
| `EXECUTOR_IMAGE_ACCOUNT_ID` | `account_id` |
| `EXECUTOR_IMAGE_PUBLISH_ROLE_ARN` | `publisher_role_arn` |
| `EXECUTOR_IMAGE_REPOSITORY_URI` | `repository_url` |

1. T12의 Dockerfile·런타임 계약을 포함한 소스 PR이 `develop`에 병합됐는지 확인한다.
   `publish-executor-image.yml`을 `develop`에서 실행하고 그 전체 커밋 SHA를 전달한다.
2. 준비 단계는 그 커밋이 `origin/develop`의 조상인지 검사한다. 해당 커밋의
   `services/cutover-executor`만 `git archive`로 추출한다. 로컬 수정·미추적 파일,
   심볼릭 링크·서브모듈·인증 파일을 빌드 문맥에 포함하지 않는다.
3. `linux/amd64` OCI 이미지, provenance, SBOM을 한 번에 만든다. Trivy가 Debian·Python
   패키지를 검사했고 수정 가능한 HIGH/CRITICAL 취약점과 시크릿 탐지 결과가 0인지 확인한다.
   이는 모든 심각도·미수정 취약점이 없다는 뜻이 아니다.
4. OCI blob의 실제 해시·크기, 전송 대상 index, 소스 label, 사용자 `10001:10001`,
   provenance·SBOM의 runtime digest 결속을 확인한 후에만 900초 OIDC 세션을 받는다.
5. `sha-<source>-run-<run-id>-<attempt>` 태그로 검사한 바이트를 그대로 복사한다.
   재실행은 새 태그를 쓰므로 이전 결과를 바꾸지 않는다. 같은 태그에 다른 digest가
   있거나 레지스트리 조회가 불명확하면 중단한다. 소비자는 태그 대신 영수증의 digest로 고정한다.
6. ECR에서 다시 받아 blob inventory를 비교하고, digest로 pull한 컨테이너에서
   네트워크 없음·읽기 전용 파일시스템·capability 제거 상태의 최소 실행 검사를 한다.
   Python·psycopg·Executor import, 비root UID/GID, pip/setuptools 부재를 확인한다.

Actions 산출물은 소스 목록과 archive hash, 제한된 scan summary, 게시 영수증이다.
원시 scan·인증 파일·운영 식별자는 공개 산출물에 넣지 않는다. 영수증의 성공은 CI 러너에서의
레지스트리 전달과 최소 실행 확인이다. **클러스터 pull, HTTPS API 기동, DB 연결, 컷오버 성공은
별도 검증**이며 이 워크플로는 해당 완료 값을 `false`로 남긴다.

## 기존 RKE2 호스트에 연결

호스트의 IAM 읽기 정책과 kubelet provider의 이미지 매칭은 서로 다른 조건이다.
`scripts/executor_provider_config.py`는 비공개 JSON 입력의 `current`와 `expected_account`를
검증해 기존 demo-chat 매칭에 동일 계정의 Executor 매칭 하나만 추가한다. 기존 binary,
환경, API 버전과 캐시 설정은 그대로이며 다른 계약이면 중단한다. 출력만 만들고
호스트 파일을 쓰거나 RKE2를 재시작하지 않는다.

Kubernetes v1.35의 [provider 등록 코드](https://github.com/kubernetes/kubernetes/blob/v1.35.0/pkg/credentialprovider/plugin/plugin.go#L129-L209)는
설정을 읽어 `matchImages`를 메모리에 보관한다. 파일 변경만으로 실행 중인 kubelet에
반영됐다고 판단하지 않는다. 실제 설치 버전과 현재 설정을 읽고, 플랫폼·T12 담당자가
진행 중인 시험과 업무 Pod 상태를 확인한 뒤 적용·재기동 시점을 맞춘다.
기존 설치 도구의 업무 Pod 보호 조건을 우회하지 않는다.

적용 후에는 실제 kubelet 경로의 digest pull을 새 Pod로 확인한다. 수동 registry token,
imagePullSecret 추가, 로컬 이미지 캐시만으로 provider 성공을 대신하지 않는다.
기존 demo-chat pull, 노드·시스템·업무 Pod 준비 상태도 함께 재확인한다.

## 로컬 검사

저장소 루트에서 Python 3.12 이상을 사용한다.

```sh
uv run --no-project --python 3.12 python -m unittest discover -s scripts/tests -p test_executor_image.py -v
uvx ruff check scripts/executor_image.py scripts/executor_provider_config.py scripts/tests/test_executor_image.py
terraform -chdir=infra/envs/executor-registry init -backend=false
terraform -chdir=infra/envs/executor-registry validate
terraform -chdir=infra/envs/executor-registry test
actionlint .github/workflows/publish-executor-image.yml
```

Terraform mock 시험은 계정·소유 태그·호스트 역할·OIDC audience가 다른 경우의 거부를 확인한다.
Python 시험은 소스 선택, OCI 변조·전송 대상 변경, scan 누락·탐지, 다른 publisher·저장소,
불변 태그 충돌과 provider 설정 보존을 확인한다. 이 시험은 실제 AWS OIDC 성공이나
클러스터 pull의 증거를 대신하지 않는다.
