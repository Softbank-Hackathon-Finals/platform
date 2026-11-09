// Checks the registration forms run before anything is sent. They mirror the SOVA MCP argument rules
// (IDENT, lengths) and add two the person should hear about before the server: no credential values,
// and repository addresses without user/token parts. The server still re-checks everything.
export const IDENT = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
// Shapes of common tokens and keys. A match blocks the field; the value is never shown back.
const SECRET = /(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(AKIA|ASIA)[A-Z0-9]{12,}|sk-[A-Za-z0-9_-]{16,}|xox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})/;
const REPO = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+?(\.git)?\/?$/;

export type Errors = Record<string, string>;

const secret = (v: string) => SECRET.test(v);
const relPath = (v: string) => !v.startsWith('/') && !v.includes('\\') && !v.includes('://') && !v.split('/').includes('..');

function common(f: Record<string, string>, e: Errors) {
  for (const [k, v] of Object.entries(f)) if (v && secret(v)) e[k] = '자격 값(토큰·키)처럼 보입니다. 등록에는 자격 값을 넣지 않습니다.';
  const name = (f.name ?? '').trim();
  if (!e.name && (name.length < 1 || name.length > 128)) e.name = '이름은 1~128자입니다.';
}

export function checkEnvironment(f: { name: string; connection_ref: string }): Errors {
  const e: Errors = {};
  common(f, e);
  if (!e.connection_ref && !IDENT.test(f.connection_ref)) {
    e.connection_ref = /^[a-z]+:\/\//i.test(f.connection_ref) || f.connection_ref.includes('/')
      ? '연결 참조는 URL이 아니라 플랫폼 운영자가 등록한 이름입니다(예: bravo-source-collector).'
      : '연결 참조는 영문·숫자·-·_ 로 된 128자 이하 이름입니다.';
  }
  return e;
}

export function checkWorkload(f: { name: string; repo_url: string; app_path: string; iac_path: string; source_environment_id: string; namespace: string }): Errors {
  const e: Errors = {};
  common(f, e);
  if (!e.repo_url) {
    if (/^https?:\/\/[^/]*@/.test(f.repo_url)) e.repo_url = '주소에 사용자 이름·토큰을 넣지 마세요. 저장소 접근은 플랫폼의 GitHub App이 합니다.';
    else if (!REPO.test(f.repo_url)) e.repo_url = 'https://github.com/조직/저장소 형식이어야 합니다.';
  }
  for (const k of ['app_path', 'iac_path'] as const) {
    if (e[k]) continue;
    if (!f[k] || f[k].length > 256) e[k] = '1~256자 경로입니다.';
    else if (!relPath(f[k])) e[k] = '저장소 안의 상대 경로입니다(/로 시작, .., 주소 불가).';
  }
  if (!f.source_environment_id) e.source_environment_id = '먼저 출발지 환경을 등록하세요.';
  if (!e.namespace && !IDENT.test(f.namespace)) e.namespace = '네임스페이스는 영문·숫자·-·_ 로 된 이름입니다.';
  return e;
}
