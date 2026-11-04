# 실제 감사 발생부터 검색까지의 로컬 자격 시험

`tests/integration/verify_live_audit_canaries.py`는 기존 T34 전달 구성과 OTLP 해석기,
T14 감사 검색기를 실제 로컬 Kubernetes/PostgreSQL 감사 기록으로 연결한다.

## 검증 경로

1. 고유 이름의 Docker k3s를 생성하고 API 서버 Metadata 감사 정책을 첫 기동 전에 지정한다.
2. 고정 이미지 digest의 CNPG와 PostgreSQL 18.4를 배포하고 `log_connections`·pgAudit DDL을 켠다.
3. 자체 생성한 클러스터의 관리자 역할로 인증서를 확인하는 TLS 접속을 실행하고 실제 `current_user`·`rolsuper`를 확인한다. DDL은 별도 비관리자 역할로 시험 schema 안에서만 실행한다.
4. 권한이 없는 전용 Role의 RoleBinding을 시험 namespace 안에서 생성·삭제한다.
5. 실제 Pod/Node UID와 파일 경로를 조회해 감사 원천을 등록한다.
6. 실제 CRI·API 서버 파일을 기존 Collector로 읽고 mTLS로 환경 내부 시험 수신기에 보낸다.
7. 전체 수신 export를 기존 해석기로 읽고 각 canary의 시간 범위로 조회해
   `AUD-01.1` 관리자 접속, `AUD-01.2` DDL, `AUD-01.3` 권한 변경 기록을 검사한다.
8. 생성한 이름의 컨테이너와 네트워크만 정리한다. 원본 로그·인증서·시험 영수증은 비공개 경로에 보존한다.

이 시험에는 합성 감사 메시지를 만들거나 누락된 로그를 채워 넣는 단계가 없다.
발생 실행의 성공 응답과 검색 성공은 별도로 확인한다. 초기화·전달·검색 중 실패하면
`INCOMPLETE`로 기록하며, 원문 오류는 비공개 로그에만 남긴다.

## 실행

```sh
uv sync --locked
uv run python tests/integration/verify_live_audit_canaries.py \
  --private-output /private/tmp/bravo-audit-unique-run
```

시험 클러스터에서만 CNPG의 관리자 접속을 활성화한다. 임시 관리자·DDL 비밀번호는 프로세스 메모리와 Pod 표준입력으로만 전달하며 공개 결과·명령 인자에 넣지 않는다. 기존 클러스터나 관리자 비밀번호를 변경하지 않는다.

Docker, Helm과 등록된 `cnpg` Helm repository가 필요하다. 기본 kubeconfig·AWS 프로필은 사용하지 않는다.
Docker 안의 시험 k3s를 실행하기 위한 privileged 옵션은 시험 인프라에만 부여하며,
감사 Collector에는 Docker 소켓·클라우드 자격증명을 연결하지 않는다. Collector는 비루트,
읽기 전용 root, capability 제거, 내부 전용 네트워크로 실행한다. 호스트 공개 포트는 없다.

비공개 출력 디렉터리는 기존 경로를 재사용하지 않으며 mode 0700/umask 0077로 만든다.
공개 증빙에는 이미지 digest·시험 결과·정리 여부만 옮긴다. 원문 로그와 canary 영수증,
CA 개인 키·TLS 키를 저장소에 올리지 않는다.

## 범위의 한계

- 실제 **로컬 k3s/CNPG** 시험이며 팀 AWS EKS와 목적지 RKE2의 실환경 완료가 아니다.
- 수신기는 파일 시험 sink다. 운영 보관 기간·불변 보관·S3 Object Lock·검색 인덱스의 가용성은 별도다.
- 일회성 원천 UID 등록이다. Pod 교체 시 재등록과 오래된 원천의 폐기 정책은 운영 호스트에서 시험해야 한다.
- 시험 DB 초기화와 비어 있는 RoleBinding 생성은 자체 생성한 클러스터에서만 실행한다.
  이 하네스를 운영 자원에 적용하는 CLI로 사용하면 안 된다.
- T19에 넣을 보안 결과의 서명·계획/사용자/원천 결속과 나머지 필수 통제는 별도다.
  감사 시험 3개 성공만으로 전체 보안 게이트 PASS나 컷오버 허가를 만들지 않는다.
