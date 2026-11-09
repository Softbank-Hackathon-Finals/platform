# sova-console-web · T21

SOVA Console를 웹(`sova.mangowhois.ai`)으로 연다. 데스크톱 앱의 화면을 그대로 쓰고, 데스크톱 main 프로세스의 일(MCP 연결,
안내자, 계획 진행 스트림)을 사람마다 이 서버가 한다. 소스는 `services/console/desktop`(`web.Dockerfile`, `src/web`).

| 경계 | 기준 |
|---|---|
| 신원 | Cloudflare Access 로그인. 서버가 토큰을 검증하고, SOVA MCP가 같은 토큰(`Bearer cfaccess.…`)을 다시 검증해 그 사람의 이미 고정된 SSO 주체로 기록한다 |
| 받는 연결 | 공개 입구(Tunnel, origin에서도 Access 토큰 요구) 파드만 |
| 나가는 연결 | DNS, SOVA MCP, Cloudflare 대역(Access 키, OpenAI API, 공개 플랫폼 Argo API) |
| 모델 키 | 플랫폼 OpenBao `platform/sova/console-web`(`api_key`) → ESO → 파일. 사람의 키·ChatGPT 로그인은 받지 않는다 |
| 데스크톱 전용 | 사람 승인(T15, 본인 SSO 증명), 고객 클러스터 Argo(본인 tailnet 신원). 웹은 그렇게 안내한다 |

## 배포

```sh
gh workflow run publish-sova-console-web-image.yml --ref develop -f source_sha=<develop 커밋>
helm upgrade --install sova-console-web infra/charts/sova-console-web -n platform-state \
  --set image.digest=sha256:… --set access.teamDomain=<team>.cloudflareaccess.com --set access.aud=<Access 앱 aud>
```

`access.aud`는 공개 입구 조정기가 만든 Access 앱의 aud다. 비워 두면 서버가 `/api`를 거부하고 처음 받은 토큰의 iss·aud만
(이메일 없이) 로그에 남기므로 그 값을 넣는다. SOVA MCP는 `webConsole.enabled=true`와 설정 Secret의 `access` 블록
(`team_domain`, `aud`, `identities: {이메일: {tenant_id, subject_id}}`)이 있어야 웹 호출을 받는다. `allowed_hosts`에
`sova-mcp.platform-state.svc:443`을 넣는다.

## 모델 키 (처음 한 번, OpenBao 관리자)

키 값은 사람이 직접 넣는다. 에이전트·Git·로그는 값을 보지 않는다.

기존 플랫폼 역할과 같은 형태로 만든다. 먼저 `bao read auth/jwt-operator/role/ai-proposal-runner`로 형태를 확인하고,
주체만 `system:serviceaccount:platform-state:sova-console-web`로 바꾼다.

```sh
bao policy write sova-console-web - <<'HCL'
path "platform/data/sova/console-web" { capabilities = ["read"] }
HCL
bao write auth/jwt-operator/role/sova-console-web role_type=jwt user_claim=sub \
  bound_audiences=bravo-platform-secrets bound_subject=system:serviceaccount:platform-state:sova-console-web \
  token_policies=sova-console-web token_ttl=10m
bao kv put platform/sova/console-web api_key=-     # 표준 입력으로 키 입력
```

그다음 `--set openai.enabled=true --set openai.openbaoCaBundle=<다른 플랫폼 SecretStore의 caBundle>`로 올린다.
