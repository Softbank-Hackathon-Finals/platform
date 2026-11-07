# T46 목적지 큐 보안 보완 검증 (로컬 k3d)

ElasticMQ는 자체 인증 · TLS가 없다(ADR-0046 5절). 앞단 보완으로 원본 SQS와 같은 질문에 같은 결과가 나오는지 로컬 k3d에서 확인한다(spec 완성 기준 10의 사전 확인). 실제 목적지 클러스터 확인은 M2에서 한다.

```sh
docker compose -f ../local/compose.yaml build runtime-base   # t46-function-runtime:local
uv run --project ../.. python prove_k8s.py                     # 클러스터 생성 · 시험 · 삭제
```

- `manifests.yaml`: ElasticMQ, TLS 종단 프록시(루트 없이 도는 `nginx-unprivileged`), 허용된 큐 클라이언트 Pod(`role: queue-client`), 허용되지 않은 Pod. 모두 루트 실행 금지 · 읽기 전용 파일시스템 · 권한 상승 금지 · 권한 전부 제거이고, 쓰기는 `/tmp` 임시 볼륨만 쓴다(CI Trivy 설정 점검 통과).
- `policies.yaml`: 수신 기본 거부, ElasticMQ 9324는 프록시만, 프록시 9443은 큐 클라이언트만.
- 인증서는 실행 때 임시 디렉터리에서 만들고 커밋하지 않는다.
- 앱 쪽은 코드 수정 없이 `AWS_ENDPOINT_URL_SQS=https://queue-tls...:9443`, `AWS_CA_BUNDLE=<CA 파일>`만 바꾼다.

## 2026-11-08 결과: PASS

정책을 걸기 전에는 다른 Pod와 평문 직접 경로가 모두 닿았다(거부가 정책 때문임을 보이는 대조).

| 시험 | 정책 적용 뒤 결과 |
|---|---|
| 허용된 클라이언트, TLS + CA | 송수신 성공 |
| 허용되지 않은 Pod, TLS + CA | 연결 실패(`EndpointConnectionError`) |
| 허용된 클라이언트, ElasticMQ 9324 평문 직접 | 연결 실패 |
| 허용된 클라이언트, 프록시에 평문 http | 거부(`ClientError`, 400) |
| 허용된 클라이언트, CA 없이 TLS | 인증서 검증 실패(`SSLError`) |

남는 위험: 허용된 Pod 안에서는 틀린 자격도 통과한다(ElasticMQ는 서명을 검사하지 않는다). 원본 SQS의 "틀린 자격 거부"와는 이 점이 다르다.
