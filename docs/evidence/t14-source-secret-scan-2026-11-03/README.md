# T14 source SEC-01.2 · 2026-11-03

**2026-11-03T09:42:50Z**에 병합된 `GitleaksProbe`를 실제 출발지 앱의 고정 소스와 애플리케이션 렌더 입력에 연결했다. 소스와 렌더 양쪽 탐지 **0건**, `SEC-01.2=PASS`이며 canonical collector bytes와 결과의 `output_sha256`를 대조했다. 로컬 scanner 실행으로 수집한 관측이다. 앱이나 클라우드를 재배포하지 않았고 새 자격증명·세션·터널을 만들지 않았다.

이번 `full` run은 **SEC-01.2 한 행만 PASS, 나머지 19행은 UNKNOWN/NOT_RUN**이다. 이전 [source SEC-01.1/IAM-01.1 관측](https://github.com/Softbank-Hackathon-Finals/platform/blob/11ae6e9ef92a5b8f0a40876ad832dd1a7c1446dc/docs/evidence/t6-runtime-2026-11-03/README.md)은 다른 시각의 별도 실행이며 이번 run으로 재작성하지 않았다. source의 세 번째 개별 통제 관측을 추가한 것이지 T14 전체 게이트나 target 동등성 통과가 아니다.

## 입력과 배포의 결속

| 입력 | 실제 사용한 범위 |
|---|---|
| 앱 소스 | `demo-chat`의 `ce5727eb3e3d2355077afdaadcb1a722aba9c7cd`를 `git archive`로 준비한 전체 tracked tree. `git ls-tree`와 파일 목록을 대조해 32개 모두 포함했으며 총 244,397 bytes다 |
| 형식/누락 | 32개 모두 UTF-8 텍스트. 빈 파일 2개도 포함, 제외 0개, 지원하지 않는 파일 0개. 비어 있는 파일만으로 구성된 입력이 아니다 |
| 이미지 | 위 소스로 만든 OCI index `sha256:001adc35a9fc767566552fb55c3c19a67b7c5efc7231fb02dc28cdeace99d0d4`. [실제 private ECR 전달 run](https://github.com/Softbank-Hackathon-Finals/demo-chat/actions/runs/41610561593)과 [출발지 배포 관측](https://github.com/Softbank-Hackathon-Finals/platform/blob/11ae6e9ef92a5b8f0a40876ad832dd1a7c1446dc/docs/evidence/t6-runtime-2026-11-03/application-deployment-readback.json)에 결속 |
| 렌더러 | 실제 앱 apply에 사용한 platform `6d8a8dfbc59dd8815d9f162e33977d7bf4b76c2d`의 `render.py`. 저장된 배포 증빙의 renderer SHA-256과 원문 바이트를 대조 |
| 렌더 입력 | 당시 보존한 application metadata의 ECR URI/digest가 인계된 registry contract 및 배포 관측과 같음을 확인. 실제 계정/ARN/저장소 URL은 비공개 유지 |
| 렌더 결과 | 위 renderer와 입력으로 재구성한 **application List 전체 6객체**, 한 JSON 파일. 선택 필터나 가림을 적용하기 전 bytes를 검사하고 파일·snapshot manifest hash를 결속 |

당시 apply 요청 본문의 사전 해시는 보존하지 않았다. 따라서 이번 렌더 파일은 **기록된 소스/입력으로 동일하게 재구성한 산출물**이며, 당시 네트워크 요청의 보존 원문이라고 주장하지 않는다. 이번에 Kubernetes API를 다시 읽지 않았으므로 배포 후 변경이나 admission/controller가 만든 전체 live 객체를 검사한 결과도 아니다. [source-binding.json](source-binding.json)에 이 경계와 기존 배포 readback의 해시를 명시했다.

이 검사는 앱 저장소의 Git 이력·이미지 파일시스템·별도 CNPG/Pooler/Helm/Terraform 산출물 전체를 검사한 것이 아니다. 실제 Secret 값을 참조 해제해 수집하지 않았다. scope는 위 앱 revision 전체와 application 렌더 List 전체다. 지원하지 않는 파일이 있었다면 제외해서 PASS를 만들지 않고 UNKNOWN으로 남겨야 한다.

## 실행 도구와 보존 방식

- 실행 라이브러리: PR #93의 병합 커밋 `6bfdd798268dc2e9f68bdc105aaebdf38c16f494`. 라이브러리나 판정 규칙은 수정하지 않았다.
- Gitleaks **8.30.1**, 승인된 실행 파일 해시 `sha256:052d18829513f92eb683b63cd15b34d845f9a7adec761fe26cd462eb3c771422`. trusted operator가 기존 설치 도구의 버전·해시와 실제 양성/앱 억제 무시/빈 입력 거부 시험을 확인한 qualification을 재사용했다. 앱이 지정한 경로나 해시를 신뢰한 것이 아니며 upstream 서명·release checksum 독립 검증은 주장하지 않는다.
- 실행 전 바이너리와 복사본을 검증하고 기본 규칙, 비어 있는 ignore 파일, `--ignore-gitleaks-allow`를 사용한다. 앱의 scanner 설정·baseline·allow 주석을 적용하지 않는다. 각 source/rendered 검사는 60초로 제한하고 subprocess에 AWS·GitHub·모델 자격증명 환경변수를 전달하지 않는다.
- 원시 보고서는 라이브러리의 비공개 임시 경로에서 처리 후 삭제된다. 공개 collector 결과에는 파일명·비밀·주변 문맥 없이 개수와 입력/출력/도구 해시만 들어간다. 파일별 manifest와 배포 metadata는 비공개에만 보존한다.
- [execution-source.json](execution-source.json)은 실행 전 library/collector/operator script·도구·설정·snapshot 해시를 기록한다. [SEC-01.2.json](SEC-01.2.json)의 canonical bytes를 바꾸지 않았으며 [control-run.json](control-run.json)의 evidence hash와 일치한다. [manifest.json](manifest.json)은 공개 artifact 5개의 bytes/hash와 비공개 사본 일치를 기록한다.

검증은 전체 tracked 입력 포함 여부, 양쪽 snapshot manifest의 독립 대조, 실행 버전/hash, `Catalog`의 결과 스키마 검증, canonical output hash와 공개 사본 bytes 일치, 공개 식별자·Gitleaks 검사다. 제품 코드를 변경하지 않아 구현을 반복하는 시험을 추가하지 않았다. 원격 서비스 인증/환경 등록 API나 격리 워커를 거친 결과는 아니며, 다음 환경에서 재사용할 때는 그 실행 경계를 별도 확인해야 한다.

재현은 승인 도구 경로/hash를 운영 설정에서 읽고, 동일 앱 커밋 전체 archive 및 비공개 배포 metadata와 고정 renderer로 두 개의 독립 입력 디렉터리를 준비한 뒤, 이 병합 커밋의 `ScanTarget`/`GitleaksProbe`를 `Runner`에 SEC-01.2로 등록하는 순서다. 임의 업로드 보고서를 trusted probe로 등록하거나 이전 관측의 시각을 새로 부여하지 않는다. 증거는 수집 시점부터 24시간의 유효기간을 가진다.
