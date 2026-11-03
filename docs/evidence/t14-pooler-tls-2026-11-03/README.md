# T14 Pooler TLS 로컬 실측

전용 로컬 kind 네임스페이스에서 PostgreSQL 18.4·CloudNativePG 1.30.1·PgBouncer 1.25.2에 실제 접속했다. 팀 AWS/RKE2의 검증 결과가 아니다.

- ENC-01.1: 인증서를 검증하는 TLS 1.3 접속과 `SELECT 1` 성공.
- ENC-01.2: 다른 CA와 다른 hostname이 각각 인증서 검증에서 거부됨. 전후 정상 접속 성공.
- ENC-01.3: `sslmode=disable`이 `SSL required`로 거부됨. 전후 정상 접속 성공.
- 잘못된 비밀번호는 정책 통과가 아닌 `UNKNOWN / POSITIVE_CONTROL_FAILED`.
- 소유한 테스트 Pooler의 평문 정책을 일시 허용했을 때 `allow`를 관측해 통제 실패로 구분. 종료 처리에서 TLS 필수로 복구.

클라이언트가 실제 앱 네임스페이스의 non-root·읽기 전용 Pod에서 실행됐다. 서비스 계정 토큰과 클라우드 자격증명은 주입하지 않았다. DB 자격은 Kubernetes Secret 참조로 주입했으며 공개 증거에는 값을 보존하지 않는다.

처음에는 PostgreSQL 18 출력과 Linux의 다중 SAN 오류 형식 때문에 검사기가 UNKNOWN을 반환했다. 형식별 회귀 시험을 추가한 뒤 새 이미지로 재검증했다. 평문 허용 음성 시험 첫 실행은 Pod 준비 지연으로 종료했으며 TLS 필수로 복구했고, 준비 대기를 고친 재실행에서 실패 검출을 확인했다.

`results.json`은 검사기 소스 해시와 정규화한 결과다. 원본 실행 증거와 자원 식별자는 비공개로 보관한다. 해시는 서명이나 제3자 인증을 뜻하지 않는다. 재현 조건과 운영 경계는 [TLS 안내](../../../services/control-runner/docs/postgres-tls.md)를 따른다.
